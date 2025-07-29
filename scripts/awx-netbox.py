#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import traceback
import requests
import json
import os
import sys
from datetime import datetime
from urllib3.exceptions import InsecureRequestWarning

# --- CONFIGURAÇÃO INICIAL ---
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

def print_flush(msg):
    print(msg, flush=True)
    sys.stdout.flush()

# Carrega e valida variáveis de ambiente
AWX_URL = os.getenv("AWX_URL")
AWX_USER = os.getenv("AWX_USER") or os.getenv("AWX_USERNAME")
AWX_PASSWORD = os.getenv("AWX_PASSWORD")
NETBOX_URL = os.getenv("NETBOX_URL") or os.getenv("NETBOX_API")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")

for var in ["AWX_URL", "AWX_USER", "AWX_PASSWORD", "NETBOX_URL", "NETBOX_TOKEN"]:
    if not locals().get(var):
        print_flush(f"ERRO CRÍTICO: Variável de ambiente obrigatória não definida: {var}")
        sys.exit(1)

# Mapeamentos
DATACENTER_TO_SITE_MAP = {"ATI-SLC-HCI": "ETIPI - Prédio Sede"}
CLUSTER_MAP = {"Cluster vSAN": "Cluster-ATI-PI-02"}

# Sessões de Requests para reutilização de conexão
awx_session = requests.Session()
awx_session.auth = (AWX_USER, AWX_PASSWORD)
awx_session.verify = False

netbox_session = requests.Session()
netbox_session.headers.update({
    "Authorization": f"Token {NETBOX_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json"
})
netbox_session.verify = False

# Cache Global para armazenar o estado do NetBox
_cache = {}

# --- FUNÇÕES DE COLETA E UTILIDADES OTIMIZADAS ---

def _paginated_get(session, base_url, endpoint, params=None):
    """Coleta todos os resultados de um endpoint paginado, tratando URLs relativas."""
    results = []
    param_str = f"&{requests.compat.urlencode(params)}" if params else ""
    url = f"{base_url}/api/{endpoint}/?limit=500{param_str}"
    
    while url:
        try:
            r = session.get(url, timeout=180)
            r.raise_for_status()
            data = r.json()
            results.extend(data.get("results", []))
            
            next_url = data.get("next")
            if next_url and next_url.startswith('/'):
                url = f"{base_url}{next_url}"
            else:
                url = next_url
                
        except requests.exceptions.RequestException as e:
            print_flush(f"ERRO: Falha ao coletar dados de {endpoint}: {e}")
            return []
    return results

def list_awx_hosts():
    """Coleta e processa os hosts do inventário 'VMware Inventory' no AWX."""
    print_flush("FASE 1: Coletando dados do AWX...")
    inventories = _paginated_get(awx_session, AWX_URL, "v2/inventories")
    vmware_inv = next((inv for inv in inventories if inv["name"] == "VMware Inventory"), None)

    if not vmware_inv:
        print_flush("ERRO: Inventário 'VMware Inventory' não encontrado.")
        return []

    hosts_raw = _paginated_get(awx_session, AWX_URL, f"v2/inventories/{vmware_inv['id']}/hosts")
    all_hosts = []
    for host in hosts_raw:
        try:
            vars_dict = json.loads(host.get("variables", "{}")) if isinstance(host.get("variables"), str) else host.get("variables", {})
            vars_dict.setdefault("vm_datacenter", "ATI-SLC-HCI")
            vars_dict.setdefault("vm_cluster", "Cluster vSAN")
            all_hosts.append(vars_dict)
        except json.JSONDecodeError:
            continue
    print_flush(f"   - Coleta do AWX concluída: {len(all_hosts)} VMs encontradas.")
    return all_hosts

def slugify(text):
    text = text.lower()
    return "".join(c for c in text if c.isalnum() or c == " ").replace(" ", "-")

def get_or_create_dependency(endpoint, name, extra_payload={}):
    """Garantir a existência de um objeto de dependência (site, role, etc) usando cache."""
    cache_key = f"dep_{endpoint}"
    if cache_key not in _cache:
        _cache[cache_key] = {item['name']: item for item in _paginated_get(netbox_session, NETBOX_URL, endpoint)}

    if name in _cache[cache_key]:
        return _cache[cache_key][name]['id']
    
    payload = {"name": name, "slug": slugify(name), **extra_payload}
    try:
        response = netbox_session.post(f"{NETBOX_URL}/api/{endpoint}/", json=payload, timeout=60)
        response.raise_for_status()
        new_obj = response.json()
        _cache[cache_key][name] = new_obj
        print_flush(f"   - Dependência criada: '{name}' em '{endpoint}'")
        return new_obj['id']
    except requests.exceptions.RequestException as e:
        print_flush(f"   - ERRO ao criar dependência '{name}': {e.response.text}")
        return None

def bulk_api_call(endpoint, object_list, operation='post'):
    """Função genérica para realizar operações de POST, PATCH ou DELETE em lote."""
    if not object_list:
        return []
    
    op_map = {
        'post': (netbox_session.post, "CRIANDO"), 'patch': (netbox_session.patch, "ATUALIZANDO"),
        'delete': (netbox_session.delete, "DELETANDO")
    }
    method, action_str = op_map[operation]
    
    created_objects = []
    batch_size = 100
    
    for i in range(0, len(object_list), batch_size):
        batch = object_list[i:i+batch_size]
        print_flush(f"   - {action_str} lote de {len(batch)} objetos em /api/{endpoint}/...")
        try:
            response = method(f"{NETBOX_URL}/api/{endpoint}/", json=batch, timeout=180)
            response.raise_for_status()
            if response.status_code != 204:
                 created_objects.extend(response.json())
        except requests.exceptions.RequestException as e:
            # --- SEÇÃO DE LOG MELHORADA ---
            print_flush(f"ERRO: Falha no lote de {action_str} para {endpoint}.")
            if e.response:
                print_flush(f"      Status Code: {e.response.status_code}")
                try:
                    # Imprime a mensagem de erro detalhada que o NetBox envia
                    print_flush(f"      Detalhes do Erro NetBox: {json.dumps(e.response.json(), indent=2, ensure_ascii=False)}")
                except json.JSONDecodeError:
                    print_flush(f"      Resposta Bruta (não JSON): {e.response.text}")
            else:
                print_flush(f"      Erro de Conexão: {e}")
            
            # Imprime o lote de dados exato que causou a falha
            print_flush("      --- Lote de Dados com Falha ---")
            print_flush(json.dumps(batch, indent=2, ensure_ascii=False))
            print_flush("      --- Fim do Lote de Dados ---")
            # --- FIM DA SEÇÃO MELHORADA ---

    return created_objects

# === EXECUÇÃO PRINCIPAL OTIMIZADA ===
def main():
    start_time = datetime.now()
    print_flush("INICIANDO SINCRONIZAÇÃO COMPLETA E OTIMIZADA...")

    vms_from_awx = list_awx_hosts()
    if not vms_from_awx:
        print_flush("Nenhuma VM para processar. Encerrando.")
        return

    print_flush("\nFASE 2: Carregando estado atual do NetBox para o cache...")
    _cache['vms'] = {vm['name']: vm for vm in _paginated_get(netbox_session, NETBOX_URL, "virtualization/virtual-machines")}
    _cache['tags'] = {tag['slug']: tag for tag in _paginated_get(netbox_session, NETBOX_URL, "extras/tags")}
    print_flush(f"   - Cache carregado: {len(_cache['vms'])} VMs, {len(_cache['tags'])} Tags.")
    
    print_flush("\nFASE 3: Preparando lotes de criação e atualização de VMs...")
    vms_to_create, vms_to_update = [], []

    for vm_data in vms_from_awx:
        vm_name = vm_data.get("vm_name")
        if not vm_name: continue

        site_id = get_or_create_dependency("dcim/sites", DATACENTER_TO_SITE_MAP.get(vm_data["vm_datacenter"]), {"status": "active"})
        cluster_type_id = get_or_create_dependency("virtualization/cluster-types", "VMware vSphere")
        cluster_id = get_or_create_dependency("virtualization/clusters", CLUSTER_MAP.get(vm_data["vm_cluster"]), {"type": cluster_type_id, "site": site_id})
        role_name = next((tag.get('name') for tag in vm_data.get("vm_tags", []) if tag.get('category') == 'Função'), None)
        role_id = get_or_create_dependency("dcim/device-roles", role_name, {"color": "00bcd4", "vm_role": True}) if role_name else None
        
        tag_ids = []
        for tag in vm_data.get("vm_tags", []):
            tag_slug = slugify(f"{tag.get('category', '')}-{tag.get('name', '')}")
            if tag_slug not in _cache['tags']:
                tag_id = get_or_create_dependency("extras/tags", tag.get('name', ''), {"description": tag.get('description', ''), "slug": tag_slug})
                if tag_id: _cache['tags'][tag_slug] = {'id': tag_id} # Atualiza cache
            if tag_slug in _cache['tags']:
                tag_ids.append(_cache['tags'][tag_slug]['id'])
                
        payload = {
            "name": vm_name, "status": "active" if vm_data.get("vm_power_state") != "poweredOff" else "offline",
            "vcpus": vm_data.get("vm_cpu_count"), "memory": int(vm_data.get("vm_memory_mb", 0)), "disk": int(vm_data.get("vm_disk_total_gb", 0)),
            "site": site_id, "cluster": cluster_id, "role": role_id, "tags": tag_ids,
            "comments": f"Última atualização via AWX: {start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        }

        if vm_name in _cache['vms']:
            payload["id"] = _cache['vms'][vm_name]["id"]
            vms_to_update.append(payload)
        else:
            vms_to_create.append(payload)

    print_flush("\nFASE 4: Executando operações em lote para VMs...")
    created_vms = bulk_api_call("virtualization/virtual-machines", vms_to_create, 'post')
    bulk_api_call("virtualization/virtual-machines", vms_to_update, 'patch')
    
    for vm in created_vms:
        _cache['vms'][vm['name']] = vm
        
    print_flush("\nFASE 5: Preparando e executando lotes para Interfaces e IPs...")
    interfaces_to_create, ips_to_create, primary_ips_to_update = [], [], []
    
    existing_interfaces = {(iface['virtual_machine']['id'], iface['name']): iface for iface in _paginated_get(netbox_session, NETBOX_URL, "virtualization/interfaces")}
    existing_ips = {ip['address']: ip for ip in _paginated_get(netbox_session, NETBOX_URL, "ipam/ip-addresses")}

    for vm_data in vms_from_awx:
        vm_name = vm_data.get("vm_name")
        if vm_name not in _cache['vms']: continue
        
        vm_id = _cache['vms'][vm_name]['id']
        interface_name = "eth0"
        
        if (vm_id, interface_name) in existing_interfaces:
            interface_id = existing_interfaces[(vm_id, interface_name)]['id']
        else:
            interfaces_to_create.append({"name": interface_name, "virtual_machine": vm_id, "type": "1000base-t"})
            continue 

        # --- CORREÇÃO APLICADA AQUI ---
        ip_addresses = vm_data.get("vm_ip_addresses", [])
        if not ip_addresses:
            # Pula para a próxima VM se não houver IPs para processar
            continue

        ip_address_str = ip_addresses[0]
        # --- FIM DA CORREÇÃO ---
        
        if ip_address_str:
            ip_with_mask = f"{ip_address_str}/32" if "/" not in ip_address_str else ip_address_str
            if ip_with_mask not in existing_ips:
                ips_to_create.append({"address": ip_with_mask, "status": "active", "assigned_object_type": "virtualization.vminterface", "assigned_object_id": interface_id})
            
            if ip_with_mask in existing_ips and _cache['vms'][vm_name].get('primary_ip4', {}).get('id') != existing_ips[ip_with_mask]['id']:
                 primary_ips_to_update.append({"id": vm_id, "primary_ip4": existing_ips[ip_with_mask]['id']})

    bulk_api_call("virtualization/interfaces", interfaces_to_create, 'post')
    bulk_api_call("ipam/ip-addresses", ips_to_create, 'post')
    bulk_api_call("virtualization/virtual-machines", primary_ips_to_update, 'patch')
    
    end_time = datetime.now()
    print_flush("\nSINCRONIZAÇÃO CONCLUÍDA!")
    print_flush(f"   - Duração total: {end_time - start_time}")
    print_flush(f"   - VMs Criadas: {len(vms_to_create)}")
    print_flush(f"   - VMs Atualizadas: {len(vms_to_update)}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print_flush(f"\nERRO FATAL NO SCRIPT: {e}")
        traceback.print_exc()
        sys.exit(1)