import os
import time
import psycopg2
import subprocess
from starrs import *
from proxmox import *
from proxmoxer import ProxmoxAPI
from flask import Flask, render_template, request, redirect, send_from_directory

app = Flask(__name__)

config = os.path.join(app.config.get('ROOT_DIR', os.getcwd()), "config.py")

app.config.from_pyfile(config)

app.config["GIT_REVISION"] = subprocess.check_output(
    ['git', 'rev-parse', '--short', 'HEAD']).decode('utf-8').rstrip()

user = 'proxstar'
proxmox = connect_proxmox(app.config['PROXMOX_HOST'],
                          app.config['PROXMOX_USER'],
                          app.config['PROXMOX_PASS'])
starrs = connect_starrs(
    app.config['STARRS_DB_NAME'], app.config['STARRS_DB_USER'],
    app.config['STARRS_DB_HOST'], app.config['STARRS_DB_PASS'])


@app.route("/")
def list_vms():
    vms = get_vms_for_user(proxmox, user)
    for vm in vms:
        if 'name' not in vm:
            vms.remove(vm)
    vms = sorted(vms, key=lambda k: k['name'])
    return render_template('list_vms.html', username='com6056', vms=vms)


@app.route("/vm/<string:vmid>")
def vm_details(vmid):
    if int(vmid) in get_user_allowed_vms(proxmox, user):
        vm = get_vm(proxmox, vmid)
        vm['vmid'] = vmid
        vm['config'] = get_vm_config(proxmox, vmid)
        vm['disks'] = get_vm_disks(proxmox, vmid, config=vm['config'])
        vm['interfaces'] = get_vm_interfaces(
            proxmox, vm['vmid'], config=vm['config'])
        return render_template('vm_details.html', username='com6056', vm=vm)
    else:
        return '', 403


@app.route("/vm/<string:vmid>/power/<string:action>", methods=['POST'])
def vm_power(vmid, action):
    if int(vmid) in get_user_allowed_vms(proxmox, user):
        change_vm_power(proxmox, vmid, action)
        return '', 200
    else:
        return '', 403


@app.route("/vm/<string:vmid>/delete", methods=['POST'])
def delete(vmid):
    if int(vmid) in get_user_allowed_vms(proxmox, user):
        vmname = get_vm_config(proxmox, vmid)['name']
        delete_vm(proxmox, starrs, vmid)
        delete_starrs(starrs, vmname)
        return '', 200
    else:
        return '', 403


@app.route("/vm/create", methods=['GET', 'POST'])
def create():
    if request.method == 'GET':
        usage = get_user_usage(proxmox, 'proxstar')
        limits = get_user_usage_limits(user)
        full_limits = check_user_limit(proxmox, user, usage, limits)
        percents = get_user_usage_percent(proxmox, usage, limits)
        return render_template(
            'create.html',
            username='com6056',
            usage=usage,
            limits=limits,
            full_limits=full_limits,
            percents=percents)
    elif request.method == 'POST':
        name = request.form['name']
        cores = request.form['cores']
        memory = request.form['memory']
        disk = request.form['disk']
        usage_check = check_user_usage(proxmox, user, cores, memory, disk)
        if usage_check:
            return usage_check
        else:
            vmid, mac = create_vm(proxmox, starrs, user, name, cores, memory,
                                  disk)
            register_starrs(starrs, name, user, mac,
                            get_next_ip(starrs,
                                        app.config['STARRS_IP_RANGE'])[0][0])
            return redirect("/proxstar/vm/{}".format(vmid))


@app.route('/novnc/<path:path>')
def send_novnc(path):
    return send_from_directory('static/novnc-pve/novnc', path)


if __name__ == "__main__":
    app.run(debug=True)
