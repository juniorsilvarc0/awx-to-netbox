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

# --- INÍCIO DAS NOVAS ALTERAÇÕES ---
# Mapeamento de Datacenter (AWX) para Site (NetBox)
DATACENTER_TO_SITE_MAP = {
    "ATI-SLC-HCI": "ETIPI - Prédio Sede"
}

# Mapeamento de Cluster (AWX) para Cluster (NetBox)
CLUSTER_MAP = {
    "Cluster vSAN": "Cluster-ATI-PI-02"
}
# --- FIM DAS NOVAS ALTERAÇÕES ---

# Verificar se as variáveis necessárias estão definidas
if not AWX_USER:
    print("Erro: AWX_USERNAME nao definido!")
    exit(1)
if not AWX_PASSWORD:
    print("Erro: AWX_PASSWORD nao definido!")
    exit(1)
if not NETBOX_URL:
    print("Erro: NETBOX_API nao definido!")
    exit(1)
if not NETBOX_TOKEN:
    print("Erro: NETBOX_TOKEN nao definido!")
    exit(1)

print_flush(f"Configuracao:")
print_flush(f"   AWX URL: {AWX_URL}")
print_flush(f"   AWX User: {AWX_USER}")
print_flush(f"   NetBox URL: {NETBOX_URL}")
print_flush(f"   NetBox Token: {'*' * 20}")
print_flush("")

INTERFACE_TYPE = "1000base-t"

HEADERS = {
    "Authorization": f"Token {NETBOX_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

# Cache para reduzir chamadas à API
_cache = {
    "sites": {},
    "clusters": {},
    "cluster_types": {},
    "device_roles": {},
    "existing_vms": None,
    "existing_interfaces": None,
    "existing_ips": None,
    "existing_tags": None
}

# === CLASSE PARA COLETA DO AWX (sem alterações) ===
class SimpleAWXCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (AWX_USER, AWX_PASSWORD)
        self.session.verify = False

    def list_hosts(self):
        print_flush("Conectando ao AWX...")
        try:
            inv_url = f"{AWX_URL}/api/v2/inventories/"
            invs = self._paginated_get(inv_url)
            if not invs:
                print_flush("Nenhum inventario encontrado")
                return []

            vmware_inv = None
            for inv in invs:
                if inv["name"] == "VMware Inventory":
                    vmware_inv = inv
                    break

            if not vmware_inv:
                print_flush("Inventario 'VMware Inventory' nao encontrado!")
                available_invs = [inv["name"] for inv in invs]
                print_flush(f"   Inventários disponíveis: {available_invs}")
                return []

            inv_id = vmware_inv["id"]
            inv_name = vmware_inv["name"]
            print_flush(f"Coletando hosts do inventario '{inv_name}' (ID {inv_id})")

            try:
                url = f"{AWX_URL}/api/v2/inventories/{inv_id}/hosts/"
                hosts_raw = self._paginated_get(url)
                print_flush(f"   └─ Encontrados {len(hosts_raw)} hosts")

                all_hosts = []
                for host in hosts_raw:
                    try:
                        vars_raw = host.get("variables", "{}")
                        if isinstance(vars_raw, str):
                            vars_dict = json.loads(vars_raw)
                        else:
                            vars_dict = vars_raw
                    except json.JSONDecodeError:
                        print_flush(f"Ignorando host {host['name']} - variaveis invalidas")
                        continue

                    # Adiciona valores padrão se ausentes, para consistência
                    if "vm_datacenter" not in vars_dict:
                        vars_dict["vm_datacenter"] = "ATI-SLC-HCI"
                    if "vm_cluster" not in vars_dict:
                        vars_dict["vm_cluster"] = "Cluster vSAN"

                    all_hosts.append(vars_dict)

            except Exception as e:
                print_flush(f"Erro ao processar inventario {inv_name}: {e}")
                return []

            print_flush(f"Total de VMs encontradas: {len(all_hosts)}")
            return all_hosts

        except Exception as e:
            print_flush(f"Erro fatal na coleta do AWX: {e}")
            raise

    def _paginated_get(self, url):
        results = []
        page = 1
        total_count = None

        if "page_size" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}page_size=100"

        while url:
            try:
                if page == 1 or page % 5 == 0:
                    print_flush(f"   └─ AWX Página {page}: coletando...")

                # ALTERADO: Timeout de 120 para 60 segundos
                r = self.session.get(url, timeout=60)
                r.raise_for_status()
                data = r.json()

                if page == 1 and "count" in data:
                    total_count = data["count"]
                    print_flush(f"   └─ Total esperado: {total_count} hosts")

                page_results = data.get("results", [])
                results.extend(page_results)

                next_url = data.get("next")
                if next_url:
                    if next_url.startswith('/'):
                        next_url = f"{AWX_URL}{next_url}"
                    url = next_url
                else:
                    url = None

                if page % 5 == 0:
                    print_flush(f"   └─ AWX Página {page}: +{len(page_results)} itens, total: {len(results)}")

                page += 1

                if total_count and len(results) >= total_count:
                    print_flush(f"   └─ Todos os {total_count} hosts coletados")
                    break

                if page > 1000:
                    print_flush(f"AVISO: AWX: Limite de páginas atingido")
                    break

            except requests.exceptions.Timeout:
                print_flush(f"AVISO: AWX Timeout na página {page} - tentando continuar...")
                if url and "page=" in url:
                    import re
                    page_match = re.search(r'page=(\d+)', url)
                    if page_match:
                        current_page = int(page_match.group(1))
                        new_page = current_page + 1
                        url = re.sub(r'page=\d+', f'page={new_page}', url)
                        continue
                break
            except requests.exceptions.RequestException as e:
                print_flush(f"ERRO: AWX Erro na requisição página {page}: {e}")
                break
            except json.JSONDecodeError as e:
                print_flush(f"ERRO: AWX Erro ao decodificar JSON página {page}: {e}")
                break
            except Exception as e:
                print_flush(f"ERRO: AWX Erro inesperado página {page}: {e}")
                break

        if total_count:
            print_flush(f"SUCESSO: AWX: {len(results)}/{total_count} itens coletados em {page-1} páginas")
        else:
            print_flush(f"SUCESSO: AWX: {len(results)} itens coletados em {page-1} páginas")

        return results

# === FUNÇÕES DE REGISTRO NO NETBOX (sem alterações) ===

def slugify(text):
    """Gera um slug simples a partir de um texto."""
    if not text:
        return ""
    text = text.lower()
    replacements = {" ": "-", "ã": "a", "á": "a", "â": "a", "à": "a", "ç": "c", "é": "e", "ê": "e", "í": "i", "ó": "o", "ô": "o", "õ": "o", "ú": "u"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text

def ensure_site(site_name):
    """Garante que um Site exista no NetBox, usando cache."""
    if not site_name:
        return None
    if site_name in _cache["sites"]:
        return _cache["sites"][site_name]

    try:
        url_get = f"{NETBOX_URL}/api/dcim/sites/?name={site_name}"
        # ALTERADO: Timeout de 30 para 60 segundos
        response = requests.get(url_get, headers=HEADERS, verify=False, timeout=60)
        response.raise_for_status()
        resultados = response.json().get('results', [])

        if resultados:
            site_id = resultados[0]['id']
            print_flush(f"   SUCESSO: Site '{site_name}' já existe. ID: {site_id}")
            _cache["sites"][site_name] = site_id
            return site_id
        else:
            print_flush(f"   AVISO: Site '{site_name}' não encontrado. A criar...")
            url_post = f"{NETBOX_URL}/api/dcim/sites/"
            payload = {"name": site_name, "slug": slugify(site_name), "status": "active"}
            # ALTERADO: Timeout de 30 para 60 segundos
            response_post = requests.post(url_post, headers=HEADERS, json=payload, verify=False, timeout=60)
            response_post.raise_for_status()
            novo_site = response_post.json()
            site_id = novo_site['id']
            print_flush(f"   SUCESSO: Site '{site_name}' criado com sucesso. ID: {site_id}")
            _cache["sites"][site_name] = site_id
            return site_id
    except Exception as e:
        print_flush(f"   ERRO: Erro ao garantir o site '{site_name}': {e}")
        return None

def ensure_cluster_type(type_name="VMware vSphere"):
    """Garante que um tipo de cluster exista."""
    if type_name in _cache["cluster_types"]:
        return _cache["cluster_types"][type_name]

    try:
        url_get = f"{NETBOX_URL}/api/virtualization/cluster-types/?name={type_name}"
        # ALTERADO: Timeout de 30 para 60 segundos
        response = requests.get(url_get, headers=HEADERS, verify=False, timeout=60)
        response.raise_for_status()
        resultados = response.json().get('results', [])
        if resultados:
            type_id = resultados[0]['id']
            _cache["cluster_types"][type_name] = type_id
            return type_id
        else:
            url_post = f"{NETBOX_URL}/api/virtualization/cluster-types/"
            payload = {"name": type_name, "slug": slugify(type_name)}
            # ALTERADO: Timeout de 30 para 60 segundos
            response_post = requests.post(url_post, headers=HEADERS, json=payload, verify=False, timeout=60)
            response_post.raise_for_status()
            novo_tipo = response_post.json()
            type_id = novo_tipo['id']
            _cache["cluster_types"][type_name] = type_id
            return type_id
    except Exception as e:
        print_flush(f"   ERRO: Erro ao garantir o tipo de cluster '{type_name}': {e}")
        return None

def ensure_cluster(cluster_name, site_id):
    """Garante que um Cluster exista no NetBox."""
    if not cluster_name:
        return None
    cache_key = f"{cluster_name}_{site_id}"
    if cache_key in _cache["clusters"]:
        return _cache["clusters"][cache_key]

    type_id = ensure_cluster_type()
    if not type_id: return None

    try:
        url_get = f"{NETBOX_URL}/api/virtualization/clusters/?name={cluster_name}"
        # ALTERADO: Timeout de 30 para 60 segundos
        response = requests.get(url_get, headers=HEADERS, verify=False, timeout=60)
        response.raise_for_status()
        resultados = response.json().get('results', [])
        if resultados:
            cluster_id = resultados[0]['id']
            _cache["clusters"][cache_key] = cluster_id
            return cluster_id
        else:
            url_post = f"{NETBOX_URL}/api/virtualization/clusters/"
            payload = {"name": cluster_name, "type": type_id, "site": site_id}
            # ALTERADO: Timeout de 30 para 60 segundos
            response_post = requests.post(url_post, headers=HEADERS, json=payload, verify=False, timeout=60)
            response_post.raise_for_status()
            novo_cluster = response_post.json()
            cluster_id = novo_cluster['id']
            _cache["clusters"][cache_key] = cluster_id
            return cluster_id
    except Exception as e:
        print_flush(f"   ERRO: Erro ao garantir o cluster '{cluster_name}': {e}")
        return None

def ensure_device_role(role_name):
    """Garante que uma Função de Dispositivo exista no NetBox, usando cache."""
    if not role_name:
        return None

    if role_name in _cache["device_roles"]:
        return _cache["device_roles"][role_name]

    try:
        url_get = f"{NETBOX_URL}/api/dcim/device-roles/?name={role_name}"
        # ALTERADO: Timeout de 30 para 60 segundos
        response = requests.get(url_get, headers=HEADERS, verify=False, timeout=60)
        response.raise_for_status()
        resultados = response.json().get('results', [])

        if resultados:
            role_id = resultados[0]['id']
            print_flush(f"   SUCESSO: Função '{role_name}' já existe. ID: {role_id}")
            _cache["device_roles"][role_name] = role_id
            return role_id
        else:
            print_flush(f"   AVISO: Função '{role_name}' não encontrada. A criar...")
            url_post = f"{NETBOX_URL}/api/dcim/device-roles/"
            payload = {
                "name": role_name,
                "slug": slugify(role_name),
                "color": "00bcd4",
                "vm_role": True
            }
            # ALTERADO: Timeout de 30 para 60 segundos
            response_post = requests.post(url_post, headers=HEADERS, json=payload, verify=False, timeout=60)
            response_post.raise_for_status()
            nova_funcao = response_post.json()
            role_id = nova_funcao['id']
            print_flush(f"   SUCESSO: Função '{role_name}' criada com sucesso. ID: {role_id}")
            _cache["device_roles"][role_name] = role_id
            return role_id

    except requests.exceptions.RequestException as e:
        print_flush(f"   ERRO: Erro ao comunicar com NetBox para gerir a função: {e}")
        return None

def ensure_vm(vm, role_id, site_id, cluster_id):
    name = vm.get("vm_name")
    vm_power_state = vm.get("vm_power_state", "")
    status = "offline" if vm_power_state == "poweredOff" else "active"

    payload = {
        "name": name,
        "vcpus": vm.get("vm_cpu_count"),
        "memory": int(vm.get("vm_memory_mb")),
        "disk": int(vm.get("vm_disk_total_gb")),
        "status": status,
        "site": site_id,
        "cluster": cluster_id,
        "comments": f"Atualizado via AWX em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    }

    if role_id:
        payload['role'] = role_id

    existing_vm = _cache["existing_vms"].get(name)
    if existing_vm:
        vm_id = existing_vm["id"]
        # ALTERADO: Adicionado timeout de 60 segundos
        requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, data=json.dumps(payload), verify=False, timeout=60)
        print_flush(f"ATUALIZADA: VM atualizada: {name}")
    else:
        # ALTERADO: Adicionado timeout de 60 segundos
        r = requests.post(f"{NETBOX_URL}/api/virtualization/virtual-machines/", headers=HEADERS, data=json.dumps(payload), verify=False, timeout=60)
        if r.status_code in [200, 201]:
            vm_id = r.json()["id"]
            _cache["existing_vms"][name] = {"id": vm_id, "name": name}
            print_flush(f"CRIADA: VM criada: {name}")
        else:
            print_flush(f"ERRO: Falha ao criar VM {name}: {r.text}")
            return None

    return vm_id

def paginated_get_all(endpoint, query=""):
    base_url = f"{NETBOX_URL}/api/{endpoint}/"
    url = f"{base_url}?limit=200{query}"
    results = []
    page = 1
    total_count = None

    print_flush(f"PAGINANDO: {endpoint}...")

    while url:
        try:
            if page == 1 or page % 5 == 0:
                print_flush(f"   └─ Página {page}: {len(results)} itens coletados...")

            r = requests.get(url, headers=HEADERS, verify=False, timeout=60)
            r.raise_for_status()
            data = r.json()

            if page == 1 and "count" in data:
                total_count = data["count"]
                print_flush(f"   └─ Total esperado: {total_count} itens")

            page_results = data.get("results", [])
            results.extend(page_results)

            next_url = data.get("next")
            if next_url:
                if next_url.startswith('/'):
                    next_url = f"{NETBOX_URL}{next_url}"
                if "limit=" not in next_url:
                    separator = "&" if "?" in next_url else "?"
                    next_url = f"{next_url}{separator}limit=200"
                url = next_url
            else:
                url = None

            if page % 5 == 0:
                print_flush(f"   └─ Página {page}: +{len(page_results)} itens, total: {len(results)}")

            page += 1

            if total_count and len(results) >= total_count:
                print_flush(f"   └─ Todos os {total_count} itens coletados")
                break

            if page > 2000:
                print_flush(f"AVISO: Limite de páginas atingido para {endpoint}")
                break

        except requests.exceptions.Timeout:
            print_flush(f"AVISO: Timeout na página {page} - tentando continuar...")
            if url and "offset=" in url:
                import re
                offset_match = re.search(r'offset=(\d+)', url)
                if offset_match:
                    current_offset = int(offset_match.group(1))
                    new_offset = current_offset + 200
                    url = re.sub(r'offset=\d+', f'offset={new_offset}', url)
                    continue
            break
        except requests.exceptions.RequestException as e:
            print_flush(f"ERRO: Erro na requisição página {page}: {e}")
            break
        except json.JSONDecodeError as e:
            print_flush(f"ERRO: Erro ao decodificar JSON página {page}: {e}")
            break
        except Exception as e:
            print_flush(f"ERRO: Erro inesperado página {page}: {e}")
            break

    if total_count:
        print_flush(f"SUCESSO: {endpoint}: {len(results)}/{total_count} itens coletados em {page-1} páginas")
    else:
        print_flush(f"SUCESSO: {endpoint}: {len(results)} itens coletados em {page-1} páginas")

    return results

def ensure_tag(tag_name, tag_category, tag_description=""):
    tag_slug = slugify(f"{tag_category}-{tag_name}")

    if tag_slug in _cache["existing_tags"]:
        return _cache["existing_tags"][tag_slug]["id"]

    tag_data = { "name": tag_name, "slug": tag_slug, "description": tag_description if tag_description else f"Categoria: {tag_category}" }

    try:
        # ALTERADO: Timeout de 30 para 60 segundos
        r = requests.post(f"{NETBOX_URL}/api/extras/tags/", headers=HEADERS, data=json.dumps(tag_data), verify=False, timeout=60)
        if r.status_code in [200, 201]:
            created_tag = r.json()
            tag_id = created_tag["id"]
            _cache["existing_tags"][tag_slug] = created_tag
            print_flush(f"   TAG CRIADA: {tag_name}")
            return tag_id
        else:
            print_flush(f"ERRO: Falha ao criar tag {tag_name}: {r.text}")
            return None
    except Exception as e:
        print_flush(f"ERRO: Erro ao criar tag {tag_name}: {e}")
        return None

def update_vm_tags(vm_id, tag_ids):
    try:
        # ALTERADO: Timeout de 30 para 60 segundos
        r = requests.get(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, verify=False, timeout=60)
        if r.status_code == 200:
            vm_data = r.json()
            existing_tag_ids = [tag["id"] for tag in vm_data.get("tags", [])]

            all_tag_ids = list(set(existing_tag_ids + tag_ids))

            update_data = {"tags": all_tag_ids}

            # ALTERADO: Timeout de 30 para 60 segundos
            r_update = requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/",
                                    headers=HEADERS, data=json.dumps(update_data), verify=False, timeout=60)

            if r_update.status_code == 200:
                new_tags_count = len(tag_ids) - len(set(tag_ids).intersection(set(existing_tag_ids)))
                if new_tags_count > 0:
                    print_flush(f"   TAGS: {new_tags_count} novas tags adicionadas à VM")
                return True
        return False
    except Exception as e:
        print_flush(f"ERRO: Erro ao atualizar tags da VM: {e}")
        return False

def ensure_interface(vm_id, name):
    interface_key = f"{vm_id}_{name}"
    if interface_key in _cache["existing_interfaces"]:
        return _cache["existing_interfaces"][interface_key]["id"]

    payload = { "name": name, "virtual_machine": vm_id, "type": INTERFACE_TYPE }
    try:
        # ALTERADO: Timeout de 30 para 60 segundos
        r = requests.post(f"{NETBOX_URL}/api/virtualization/interfaces/", headers=HEADERS, data=json.dumps(payload), verify=False, timeout=60)
        if r.status_code in [200, 201]:
            interface_data = r.json()
            interface_id = interface_data["id"]
            _cache["existing_interfaces"][interface_key] = interface_data
            return interface_id
        else:
            print_flush(f"ERRO: Falha ao criar interface {name}: {r.text}")
            return None
    except Exception as e:
        print_flush(f"ERRO: Erro ao criar interface {name}: {e}")
        return None

def ensure_ip(ip_str, interface_id):
    ip_only = ip_str.split("/")[0]

    if ip_only in _cache["existing_ips"]:
        ip_data = _cache["existing_ips"][ip_only]
        ip_id = ip_data["id"]

        current_interface = ip_data.get("assigned_object_id")
        if current_interface != interface_id:
            try:
                # ALTERADO: Timeout de 30 para 60 segundos
                requests.patch(f"{NETBOX_URL}/api/ipam/ip-addresses/{ip_id}/", headers=HEADERS, data=json.dumps({
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": interface_id
                }), verify=False, timeout=60)
                _cache["existing_ips"][ip_only]["assigned_object_id"] = interface_id
            except Exception as e:
                print_flush(f"ERRO: Erro ao atualizar associação do IP {ip_str}: {e}")

        return ip_id

    payload = { "address": ip_str, "status": "active", "assigned_object_type": "virtualization.vminterface", "assigned_object_id": interface_id }
    try:
        # ALTERADO: Timeout de 30 para 60 segundos
        r = requests.post(f"{NETBOX_URL}/api/ipam/ip-addresses/", headers=HEADERS, data=json.dumps(payload), verify=False, timeout=60)
        if r.status_code in [200, 201]:
            ip_data = r.json()
            ip_id = ip_data["id"]
            _cache["existing_ips"][ip_only] = ip_data
            return ip_id
        else:
            print_flush(f"ERRO: Falha ao criar IP {ip_str}: {r.text}")
            return None
    except Exception as e:
        print_flush(f"ERRO: Erro ao criar IP {ip_str}: {e}")
        return None

def update_primary_ip(vm_id, ip_id):
    payload = {"primary_ip4": ip_id}
    # ALTERADO: Adicionado timeout de 60 segundos
    requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, data=json.dumps(payload), verify=False, timeout=60)

# === EXECUÇÃO PRINCIPAL ===
def main():
    print_flush("INICIANDO: Sincronização AWX -> NetBox...")
    collector = SimpleAWXCollector()
    vms = collector.list_hosts()

    print_flush("CARREGANDO: Cache essencial do NetBox...")

    try:
        print_flush("   CARREGANDO: VMs...")
        all_vms = paginated_get_all("virtualization/virtual-machines")
        _cache["existing_vms"] = {vm["name"]: vm for vm in all_vms}
        print_flush(f"   └─ SUCESSO: {len(_cache['existing_vms'])} VMs carregadas")
    except Exception as e:
        print_flush(f"ERRO: Erro ao carregar VMs: {e}")
        _cache["existing_vms"] = {}

    _cache["existing_interfaces"] = {}
    _cache["existing_ips"] = {}
    _cache["existing_tags"] = {}

    print_flush(f"SUCESSO: Cache essencial carregado! Outros caches serão populados sob demanda.")

    print_flush(f"PROCESSANDO: {len(vms)} VMs completas (VM + Interface + IP + Tags + Função)...")
    success_count = 0
    error_count = 0

    batch_size = 10

    for i, vm in enumerate(vms, 1):
        try:
            vm_name = vm.get("vm_name")
            if not vm_name:
                continue

            if i % batch_size == 0 or i == 1 or i == len(vms):
                print_flush(f"PROGRESSO: {i}/{len(vms)} VMs ({success_count} ok, {error_count} erros)")
                if i % (batch_size * 10) == 0:
                    import time
                    time.sleep(1)

            # --- INÍCIO DA LÓGICA DE MAPEAMENTO ---
            # 1. Aplicar mapeamento de Datacenter para Site
            datacenter_original = vm.get("vm_datacenter")
            nome_do_site = DATACENTER_TO_SITE_MAP.get(datacenter_original, datacenter_original)
            site_id = ensure_site(nome_do_site)

            # 2. Aplicar mapeamento de Cluster
            cluster_original = vm.get("vm_cluster")
            nome_do_cluster = CLUSTER_MAP.get(cluster_original, cluster_original) # <-- ALTERAÇÃO
            cluster_id = ensure_cluster(nome_do_cluster, site_id)

            # 3. Extrair e garantir a Função de Dispositivo
            vm_tags_list = vm.get("vm_tags", [])
            role_name = None
            if vm_tags_list:
                for tag in vm_tags_list:
                    if tag.get('category') == 'Função':
                        role_name = tag.get('name')
                        break
            role_id = ensure_device_role(role_name)
            # --- FIM DA LÓGICA DE MAPEAMENTO ---

            # 4. Criar/atualizar VM, passando todos os IDs necessários
            vm_id = ensure_vm(vm, role_id, site_id, cluster_id)
            if not vm_id:
                error_count += 1
                continue

            # 5. Processar tags da VM (lógica existente)
            if vm_tags_list:
                tag_ids = []
                for tag in vm_tags_list:
                    tag_name = tag.get("name", "")
                    tag_category = tag.get("category", "")
                    tag_description = tag.get("description", "")

                    if tag_name and tag_category:
                        tag_id = ensure_tag(tag_name, tag_category, tag_description)
                        if tag_id:
                            tag_ids.append(tag_id)

                if tag_ids:
                    update_vm_tags(vm_id, tag_ids)

            # 6. Criar/atualizar interface eth0 (lógica existente)
            interface_id = ensure_interface(vm_id, "eth0")
            if not interface_id:
                print_flush(f"AVISO: Falha ao criar interface para VM {vm_name}")
                error_count += 1
                continue

            # 7. Processar IPs da VM (lógica existente)
            vm_ips = vm.get("vm_ip_addresses", [])
            primary_ip_id = None

            if vm_ips:
                primary_ip = vm_ips[0]
                if primary_ip and primary_ip != "":
                    if "/" not in primary_ip:
                        primary_ip = f"{primary_ip}/32"

                    primary_ip_id = ensure_ip(primary_ip, interface_id)
                    if primary_ip_id:
                        print_flush(f"SUCESSO: IP {primary_ip} associado à interface eth0 da VM {vm_name}")
                    else:
                        print_flush(f"AVISO: Falha ao associar IP {primary_ip} à VM {vm_name}")

            # 8. Definir IP primário na VM (lógica existente)
            if primary_ip_id:
                update_primary_ip(vm_id, primary_ip_id)
                print_flush(f"IP PRIMARIO: IP primário definido para VM {vm_name}")

            success_count += 1

        except KeyboardInterrupt:
            print_flush(f"\nAVISO: Interrompido pelo usuário. Processadas {i} VMs.")
            break
        except Exception as e:
            error_count += 1
            print_flush(f"ERRO: Erro ao processar VM {vm.get('vm_name')}: {e}")
            traceback.print_exc()

    print_flush(f"CONCLUIDO: Sincronização concluída! SUCESSO: {success_count} VMs processadas, ERRO: {error_count} erros")

if __name__ == "__main__":
    main()