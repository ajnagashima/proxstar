import os
import time
import requests
import paramiko
import psycopg2
from flask import Flask
from rq import get_current_job
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from proxstar.db import *
from proxstar.util import *
from proxstar.mail import *
from proxstar.starrs import *
from proxstar.vnc import send_stop_ssh_tunnel
from proxstar.vm import VM, create_vm, clone_vm
from proxstar.user import User, get_vms_for_rtp
from proxstar.proxmox import connect_proxmox, get_pools

app = Flask(__name__)
if os.path.exists(
        os.path.join(
            app.config.get('ROOT_DIR', os.getcwd()), "config.local.py")):
    config = os.path.join(
        app.config.get('ROOT_DIR', os.getcwd()), "config.local.py")
else:
    config = os.path.join(app.config.get('ROOT_DIR', os.getcwd()), "config.py")
app.config.from_pyfile(config)


def connect_db():
    engine = create_engine(app.config['SQLALCHEMY_DATABASE_URI'])
    Base.metadata.bind = engine
    DBSession = sessionmaker(bind=engine)
    db = DBSession()
    return db


def connect_starrs():
    starrs = psycopg2.connect(
        "dbname='{}' user='{}' host='{}' password='{}'".format(
            app.config['STARRS_DB_NAME'], app.config['STARRS_DB_USER'],
            app.config['STARRS_DB_HOST'], app.config['STARRS_DB_PASS']))
    return starrs


def create_vm_task(user, name, cores, memory, disk, iso):
    with app.app_context():
        job = get_current_job()
        proxmox = connect_proxmox()
        db = connect_db()
        starrs = connect_starrs()
        job.meta['status'] = 'creating VM'
        job.save_meta()
        vmid, mac = create_vm(proxmox, user, name, cores, memory, disk, iso)
        job.meta['status'] = 'registering in STARRS'
        job.save_meta()
        register_starrs(starrs, name, app.config['STARRS_USER'], mac,
                        get_next_ip(starrs, app.config['STARRS_IP_RANGE']))
        job.meta['status'] = 'setting VM expiration'
        job.save_meta()
        delete_vm_expire(db, vmid)
        get_vm_expire(db, vmid, app.config['VM_EXPIRE_MONTHS'])
        job.meta['status'] = 'complete'
        job.save_meta()


def delete_vm_task(vmid):
    with app.app_context():
        db = connect_db()
        starrs = connect_starrs()
        vm = VM(vmid)
        if vm.status != 'stopped':
            vm.stop()
            retry = 0
            while retry < 10:
                time.sleep(3)
                if vm.status == 'stopped':
                    break
                retry += 1
        vm.delete()
        delete_starrs(starrs, vm.name)
        delete_vm_expire(db, vmid)


def process_expiring_vms_task():
    with app.app_context():
        proxmox = connect_proxmox()
        db = connect_db()
        starrs = connect_starrs()
        pools = get_pools(proxmox, db)
        expired_vms = []
        for pool in pools:
            user = User(pool)
            expiring_vms = []
            vms = user.vms
            for vm in vms:
                vm = VM(vm['vmid'])
                days = (vm.expire - datetime.date.today()).days
                if days in [10, 7, 3, 1, 0, -1, -2, -3, -4, -5, -6]:
                    name = vm.name
                    expiring_vms.append([vm.id, vm.name, days])
                    if days <= 0:
                        expired_vms.append([vm.id, vm.name, days])
                        vm.stop()
                elif days <= -7:
                    print(
                        "Deleting {} ({}) as it has been at least a week since expiration.".
                        format(vm.name, vm.id))
                    send_stop_ssh_tunnel(vm.id)
                    delete_vm_task(vm.id)
            if expiring_vms:
                send_vm_expire_email(pool, expiring_vms)
        if expired_vms:
            send_rtp_vm_delete_email(expired_vms)


def generate_pool_cache_task():
    with app.app_context():
        proxmox = connect_proxmox()
        db = connect_db()
        pools = get_vms_for_rtp(proxmox, db)
        store_pool_cache(db, pools)


def setup_template_task(template_id, name, user, ssh_key, cores, memory):
    with app.app_context():
        job = get_current_job()
        proxmox = connect_proxmox()
        starrs = connect_starrs()
        db = connect_db()
        print("[{}] Retrieving template info for template {}.".format(
            name, template_id))
        template = get_template(db, template_id)
        print("[{}] Cloning template {}.".format(name, template_id))
        job.meta['status'] = 'cloning template'
        job.save_meta()
        vmid, mac = clone_vm(proxmox, template_id, name, user)
        print("[{}] Registering in STARRS.".format(name))
        job.meta['status'] = 'registering in STARRS'
        job.save_meta()
        ip = get_next_ip(starrs, app.config['STARRS_IP_RANGE'])
        register_starrs(starrs, name, app.config['STARRS_USER'], mac, ip)
        get_vm_expire(db, vmid, app.config['VM_EXPIRE_MONTHS'])
        print("[{}] Giving Proxmox some time to finish cloning.".format(name))
        job.meta['status'] = 'waiting for Proxmox'
        time.sleep(15)
        print("[{}] Setting CPU and memory.".format(name))
        job.meta['status'] = 'setting CPU and memory'
        job.save_meta()
        vm = VM(vmid)
        vm.set_cpu(cores)
        vm.set_mem(memory)
        print("[{}] Applying cloud-init config.".format(name))
        job.meta['status'] = 'applying cloud-init'
        vm.set_ci_user(user)
        vm.set_ci_ssh_key(ssh_key)
        vm.set_ci_network()
        print(
            "[{}] Waiting for STARRS to propogate before starting VM.".format(
                name))
        job.meta['status'] = 'waiting for STARRS'
        job.save_meta()
        time.sleep(90)
        print("[{}] Starting VM.".format(name))
        job.meta['status'] = 'starting VM'
        job.save_meta()
        vm.start()
        print("[{}] Template successfully provisioned.".format(name))
        job.meta['status'] = 'completed'
        job.save_meta()


def cleanup_vnc_task():
    requests.post(
        "https://{}/console/cleanup".format(app.config['SERVER_NAME']),
        data={'token': app.config['VNC_CLEANUP_TOKEN']},
        verify=False)
