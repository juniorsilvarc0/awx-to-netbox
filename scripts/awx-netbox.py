#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import traceback
import requests
import json
import os
import sys
from datetime import datetime
from urllib3.exceptions import InsecureRequestWarning

# Desabilita warnings SSL inseguros
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Função para print com flush imediato
def print_flush(msg):
    print(msg, flush=True)
    sys.stdout.flush()

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

print_flush(f"✅ Configuração:")
print_flush(f"   AWX URL: {AWX_URL}")
print_flush(f"   AWX User: {AWX_USER}")
print_flush(f"   NetBox URL: {NETBOX_URL}")
print_flush(f"   NetBox Token: {'*' * 20}")
print_flush("")

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
        print_flush("🔍 Conectando ao AWX...")
        try:
            inv_url = f"{AWX_URL}/api/v2/inventories/"
            invs = self._paginated_get(inv_url)
            if not invs:
                print_flush("❌ Nenhum inventário encontrado")
                return []

            # Procurar apenas o inventário "VMware Inventory"
            vmware_inv = None
            for inv in invs:
                if inv["name"] == "VMware Inventory":
                    vmware_inv = inv
                    break
            
            if not vmware_inv:
                print_flush("❌ Inventário 'VMware Inventory' não encontrado!")
                available_invs = [inv["name"] for inv in invs]
                print_flush(f"   Inventários disponíveis: {available_invs}")
                return []

            inv_id = vmware_inv["id"]
            inv_name = vmware_inv["name"]
            print_flush(f"📦 Coletando hosts do inventário '{inv_name}' (ID {inv_id})")
            
            try:
                url = f"{AWX_URL}/api/v2/inventories/{inv_id}/hosts/"
                hosts_raw = self._paginated_get(url)
                print_flush(f"   └─ Encontrados {len(hosts_raw)} hosts")
                
                all_hosts = []
                for host in hosts_raw:
                    try:
                        vars = json.loads(host.get("variables", "{}"))
                    except json.JSONDecodeError:
                        print_flush(f"⚠️ Ignorando host {host['name']} - variáveis inválidas")
                        continue

                    vars["vm_name"] = vars.get("vm_name", host["name"])
                    vars["vm_uuid"] = vars.get("vm_uuid", "")
                    vars["vm_ip_addresses"] = vars.get("vm_ip_addresses", [])
                    vars["vm_cluster"] = vars.get("vm_cluster", "")
                    vars["vm_cpu_count"] = vars.get("vm_cpu_count", 1)
                    vars["vm_memory_mb"] = vars.get("vm_memory_mb", 0)
                    vars["vm_disk_total_gb"] = vars.get("vm_disk_total_gb", 0)
                    all_hosts.append(vars)
                    
            except Exception as e:
                print_flush(f"❌ Erro ao processar inventário {inv_name}: {e}")
                return []

            print_flush(f"✅ Total de VMs encontradas: {len(all_hosts)}")
            return all_hosts
            
        except Exception as e:
            print_flush(f"❌ Erro fatal na coleta do AWX: {e}")
            raise

    def _paginated_get(self, url):
        results = []
        page = 1
        while url:
            try:
                print_flush(f"   └─ Página {page}: {url}")
                r = self.session.get(url)
                r.raise_for_status()
                data = r.json()
                page_results = data.get("results", [])
                results.extend(page_results)
                
                # Processar URL da próxima página
                next_url = data.get("next")
                if next_url:
                    # Se a URL é relativa, adicionar o base URL
                    if next_url.startswith('/'):
                        next_url = f"{AWX_URL}{next_url}"
                    url = next_url
                else:
                    url = None
                    
                print_flush(f"   └─ Página {page}: {len(page_results)} itens, total: {len(results)}")
                page += 1
            except requests.exceptions.RequestException as e:
                print_flush(f"❌ Erro na requisição: {e}")
                break
            except json.JSONDecodeError as e:
                print_flush(f"❌ Erro ao decodificar JSON: {e}")
                break
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
    print_flush("🚀 Iniciando sincronização AWX → NetBox...")
    collector = SimpleAWXCollector()
    vms = collector.list_hosts()

    print_flush(f"🔄 Processando {len(vms)} VMs...")
    for i, vm in enumerate(vms, 1):
        try:
            if not vm.get("vm_name"):
                continue

            print_flush(f"📝 ({i}/{len(vms)}) Processando VM: {vm.get('vm_name')}")
            
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
            print_flush(f"❌ Erro ao processar VM {vm.get('vm_name')}: {e}")
            traceback.print_exc()
            
    print_flush("🎉 Sincronização concluída!")
if __name__ == "__main__":
    main()
