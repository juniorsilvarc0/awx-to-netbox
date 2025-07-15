#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import traceback
import requests
import json
import os
from datetime import datetime
from urllib3.exceptions import InsecureRequestWarning

# Desabilita warnings SSL inseguros
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# === VARIÁVEIS DE AMBIENTE (são injetadas via credenciais do AWX) ===
AWX_URL = os.getenv("AWX_URL", "http://10.0.100.159:8013")
AWX_USER = os.getenv("AWX_USER") or os.getenv("AWX_USERNAME")
AWX_PASSWORD = os.getenv("AWX_PASSWORD")

NETBOX_URL = os.getenv("NETBOX_URL") or os.getenv("NETBOX_API")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")

# Verificar se as variáveis necessárias estão definidas
if not AWX_USER:
    print("❌ Erro: AWX_USERNAME não definido!")
    exit(1)
if not AWX_PASSWORD:
    print("❌ Erro: AWX_PASSWORD não definido!")
    exit(1)
if not NETBOX_URL:
    print("❌ Erro: NETBOX_API não definido!")
    exit(1)
if not NETBOX_TOKEN:
    print("❌ Erro: NETBOX_TOKEN não definido!")
    exit(1)

print(f"✅ Configuração:")
print(f"   AWX URL: {AWX_URL}")
print(f"   AWX User: {AWX_USER}")
print(f"   NetBox URL: {NETBOX_URL}")
print(f"   NetBox Token: {'*' * 20}")
print()

FORCE_SITE = "ATI-SLC-HCI"
INTERFACE_TYPE = "1000base-t"

HEADERS = {
    "Authorization": f"Token {NETBOX_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

# === CLASSE PARA COLETA DO AWX ===
class SimpleAWXCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (AWX_USER, AWX_PASSWORD)
        self.session.verify = False

    def list_hosts(self):
        print("🔍 Conectando ao AWX...")
        inv_url = f"{AWX_URL}/api/v2/inventories/"
        invs = self._paginated_get(inv_url)
        if not invs:
            print("❌ Nenhum inventário encontrado")
            return []

        all_hosts = []
        for inv in invs:
            inv_id = inv["id"]
            inv_name = inv["name"]
            print(f"📦 Coletando hosts do inventário '{inv_name}' (ID {inv_id})")
            url = f"{AWX_URL}/api/v2/inventories/{inv_id}/hosts/"
            hosts_raw = self._paginated_get(url)
            for host in hosts_raw:
                try:
                    vars = json.loads(host.get("variables", "{}"))
                except json.JSONDecodeError:
                    print(f"⚠️ Ignorando host {host['name']} - variáveis inválidas")
                    continue

                vars["vm_name"] = vars.get("vm_name", host["name"])
                vars["vm_uuid"] = vars.get("vm_uuid", "")
                vars["vm_ip_addresses"] = vars.get("vm_ip_addresses", [])
                vars["vm_cluster"] = vars.get("vm_cluster", "")
                vars["vm_cpu_count"] = vars.get("vm_cpu_count", 1)
                vars["vm_memory_mb"] = vars.get("vm_memory_mb", 0)
                vars["vm_disk_total_gb"] = vars.get("vm_disk_total_gb", 0)
                all_hosts.append(vars)

        print(f"✅ Total de VMs encontradas: {len(all_hosts)}")
        return all_hosts

    def _paginated_get(self, url):
        results = []
        while url:
            r = self.session.get(url)
            data = r.json()
            results.extend(data.get("results", []))
            url = data.get("next")
        return results

# === FUNÇÕES DE REGISTRO NO NETBOX ===
def paginated_get_all(endpoint, query=""):
    url = f"{NETBOX_URL}/api/{endpoint}/?limit=1000{query}"
    results = []
    while url:
        r = requests.get(url, headers=HEADERS, verify=False)
        data = r.json()
        results.extend(data.get("results", []))
        url = data.get("next")
    return results

def get_id_by_name(endpoint, name):
    entries = paginated_get_all(endpoint, f"&name={name}")
    for item in entries:
        if item["name"] == name:
            return item["id"]
    return None

def ensure_vm(vm):
    name = vm.get("vm_name")
    uuid = vm.get("vm_uuid", "")

    site_id = get_id_by_name("dcim/sites", FORCE_SITE)
    cluster_id = get_id_by_name("virtualization/clusters", vm.get("vm_cluster"))

    payload = {
        "name": name,
        "vcpus": vm.get("vm_cpu_count"),
        "memory": int(vm.get("vm_memory_mb")),
        "disk": int(vm.get("vm_disk_total_gb") * 1024),
        "status": "active",
        "site": site_id,
        "cluster": cluster_id,
        "comments": f"Atualizado via AWX em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    }

    existing = paginated_get_all("virtualization/virtual-machines", f"&name={name}")
    if existing:
        vm_id = existing[0]["id"]
        requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, data=json.dumps(payload), verify=False)
        print(f"♻️ VM atualizada: {name}")
    else:
        r = requests.post(f"{NETBOX_URL}/api/virtualization/virtual-machines/", headers=HEADERS, data=json.dumps(payload), verify=False)
        if r.status_code in [200, 201]:
            vm_id = r.json()["id"]
            print(f"✅ VM criada: {name}")
        else:
            print(f"❌ Falha ao criar VM {name}: {r.text}")
            return None

    return vm_id

def ensure_interface(vm_id, name):
    existing = paginated_get_all("virtualization/interfaces", f"&virtual_machine_id={vm_id}&name={name}")
    if existing:
        return existing[0]["id"]

    payload = {
        "name": name,
        "virtual_machine": vm_id,
        "type": INTERFACE_TYPE
    }
    r = requests.post(f"{NETBOX_URL}/api/virtualization/interfaces/", headers=HEADERS, data=json.dumps(payload), verify=False)
    return r.json().get("id")

def ensure_ip(ip_str, interface_id):
    existing = paginated_get_all("ipam/ip-addresses", f"&address={ip_str}")
    if existing:
        ip_id = existing[0]["id"]
        requests.patch(f"{NETBOX_URL}/api/ipam/ip-addresses/{ip_id}/", headers=HEADERS, data=json.dumps({
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": interface_id
        }), verify=False)
        return ip_id

    payload = {
        "address": ip_str,
        "status": "active",
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": interface_id
    }
    r = requests.post(f"{NETBOX_URL}/api/ipam/ip-addresses/", headers=HEADERS, data=json.dumps(payload), verify=False)
    return r.json().get("id")

def update_primary_ip(vm_id, ip_id):
    payload = {"primary_ip4": ip_id}
    requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, data=json.dumps(payload), verify=False)

# === EXECUÇÃO PRINCIPAL ===
def main():
    print("🚀 Iniciando sincronização AWX → NetBox...")
    collector = SimpleAWXCollector()
    vms = collector.list_hosts()

    for vm in vms:
        try:
            if not vm.get("vm_name"):
                continue

            vm_id = ensure_vm(vm)
            if not vm_id:
                continue

            iface_id = ensure_interface(vm_id, "eth0")
            if not iface_id:
                continue

            ip_list = vm.get("vm_ip_addresses", [])
            if ip_list:
                ip_id = ensure_ip(ip_list[0], iface_id)
                if ip_id:
                    update_primary_ip(vm_id, ip_id)

        except Exception as e:
            print(f"❌ Erro ao processar VM {vm.get('vm_name')}: {e}")
            traceback.print_exc()
if __name__ == "__main__":
    main()
