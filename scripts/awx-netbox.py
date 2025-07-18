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

# Cache para reduzir chamadas à API
_cache = {
    "sites": {},
    "clusters": {},
    "existing_vms": None,
    "existing_interfaces": None,
    "existing_ips": None,
    "existing_tags": None  # Adicionar cache para tags
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
                        # Processar variáveis - pode vir como string JSON
                        vars_raw = host.get("variables", "{}")
                        if isinstance(vars_raw, str):
                            vars = json.loads(vars_raw)
                        else:
                            vars = vars_raw
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
                    vars["vm_tags"] = vars.get("vm_tags", [])  # Adicionar tags
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
        """Paginação robusta para AWX com suporte a 10000+ itens"""
        results = []
        page = 1
        total_count = None
        
        # Adicionar page_size para AWX se não estiver presente
        if "page_size" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}page_size=100"  # Reduzir page_size para evitar timeouts
        
        while url:
            try:
                # Mostrar progresso apenas a cada 5 páginas
                if page == 1 or page % 5 == 0:
                    print_flush(f"   └─ AWX Página {page}: coletando...")
                
                r = self.session.get(url, timeout=120)  # Aumentar timeout
                r.raise_for_status()
                data = r.json()
                
                # Capturar total na primeira página
                if page == 1 and "count" in data:
                    total_count = data["count"]
                    print_flush(f"   └─ Total esperado: {total_count} hosts")
                
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
                    
                # Mostrar progresso a cada 5 páginas
                if page % 5 == 0:
                    print_flush(f"   └─ AWX Página {page}: +{len(page_results)} itens, total: {len(results)}")
                
                page += 1
                
                # Verificar se já coletamos todos os itens esperados
                if total_count and len(results) >= total_count:
                    print_flush(f"   └─ Todos os {total_count} hosts coletados")
                    break
                    
                # Limite de segurança
                if page > 1000:  # Aumentar limite
                    print_flush(f"⚠️ AWX: Limite de páginas atingido")
                    break
                    
            except requests.exceptions.Timeout:
                print_flush(f"⚠️ AWX Timeout na página {page} - tentando continuar...")
                # Tentar próxima página se possível
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
                print_flush(f"❌ AWX Erro na requisição página {page}: {e}")
                break
            except json.JSONDecodeError as e:
                print_flush(f"❌ AWX Erro ao decodificar JSON página {page}: {e}")
                break
            except Exception as e:
                print_flush(f"❌ AWX Erro inesperado página {page}: {e}")
                break
        
        if total_count:
            print_flush(f"✅ AWX: {len(results)}/{total_count} itens coletados em {page-1} páginas")
        else:
            print_flush(f"✅ AWX: {len(results)} itens coletados em {page-1} páginas")
        
        return results

# === FUNÇÕES DE REGISTRO NO NETBOX ===
def paginated_get_all(endpoint, query=""):
    """Função robusta para paginação do NetBox com suporte a grandes volumes"""
    # Usar limit menor para evitar timeouts
    base_url = f"{NETBOX_URL}/api/{endpoint}/"
    url = f"{base_url}?limit=200{query}"  # Reduzir limit para evitar timeouts
    results = []
    page = 1
    total_count = None
    
    print_flush(f"🔍 Paginando {endpoint}...")
    
    while url:
        try:
            # Mostrar progresso apenas a cada 5 páginas para reduzir output
            if page == 1 or page % 5 == 0:
                print_flush(f"   └─ Página {page}: {len(results)} itens coletados...")
            
            r = requests.get(url, headers=HEADERS, verify=False, timeout=60)  # Aumentar timeout
            r.raise_for_status()
            data = r.json()
            
            # Capturar total na primeira página
            if page == 1 and "count" in data:
                total_count = data["count"]
                print_flush(f"   └─ Total esperado: {total_count} itens")
            
            page_results = data.get("results", [])
            results.extend(page_results)
            
            # Processar próxima URL
            next_url = data.get("next")
            if next_url:
                # Se a URL é relativa, adicionar o base URL
                if next_url.startswith('/'):
                    next_url = f"{NETBOX_URL}{next_url}"
                # Garantir que não duplicamos parâmetros
                if "limit=" not in next_url:
                    separator = "&" if "?" in next_url else "?"
                    next_url = f"{next_url}{separator}limit=200"
                url = next_url
            else:
                url = None
                
            # Mostrar progresso a cada 5 páginas
            if page % 5 == 0:
                print_flush(f"   └─ Página {page}: +{len(page_results)} itens, total: {len(results)}")
            
            page += 1
            
            # Verificar se já coletamos todos os itens esperados
            if total_count and len(results) >= total_count:
                print_flush(f"   └─ Todos os {total_count} itens coletados")
                break
                
            # Limite de segurança para evitar loops infinitos
            if page > 2000:  # Aumentar limite para suportar mais páginas
                print_flush(f"⚠️ Limite de páginas atingido para {endpoint}")
                break
                
        except requests.exceptions.Timeout:
            print_flush(f"⚠️ Timeout na página {page} - tentando continuar...")
            # Tentar próxima página se possível
            if url and "offset=" in url:
                # Extrair e incrementar offset manualmente
                import re
                offset_match = re.search(r'offset=(\d+)', url)
                if offset_match:
                    current_offset = int(offset_match.group(1))
                    new_offset = current_offset + 200  # Assumindo limit=200
                    url = re.sub(r'offset=\d+', f'offset={new_offset}', url)
                    continue
            break
        except requests.exceptions.RequestException as e:
            print_flush(f"❌ Erro na requisição página {page}: {e}")
            break
        except json.JSONDecodeError as e:
            print_flush(f"❌ Erro ao decodificar JSON página {page}: {e}")
            break
        except Exception as e:
            print_flush(f"❌ Erro inesperado página {page}: {e}")
            break
    
    if total_count:
        print_flush(f"✅ {endpoint}: {len(results)}/{total_count} itens coletados em {page-1} páginas")
    else:
        print_flush(f"✅ {endpoint}: {len(results)} itens coletados em {page-1} páginas")
    
    return results

def get_id_by_name(endpoint, name):
    # Usar cache para sites e clusters
    if endpoint == "dcim/sites":
        if name not in _cache["sites"]:
            entries = paginated_get_all(endpoint, f"&name={name}")
            for item in entries:
                _cache["sites"][item["name"]] = item["id"]
        return _cache["sites"].get(name)
    
    elif endpoint == "virtualization/clusters":
        if name not in _cache["clusters"]:
            entries = paginated_get_all(endpoint, f"&name={name}")
            for item in entries:
                _cache["clusters"][item["name"]] = item["id"]
        return _cache["clusters"].get(name)
    
    # Fallback para outros endpoints
    entries = paginated_get_all(endpoint, f"&name={name}")
    for item in entries:
        if item["name"] == name:
            return item["id"]
    return None

def ensure_tag(tag_name, tag_category, tag_description=""):
    """Criar ou obter tag usando cache para performance"""
    # Criar slug da tag - mantém categoria para unicidade
    tag_slug = f"{tag_category}-{tag_name}".lower()
    # Remover caracteres especiais
    replacements = {
        " ": "-", "ã": "a", "ç": "c", "á": "a", "é": "e", 
        "í": "i", "ó": "o", "ú": "u", "â": "a", "ê": "e",
        "ô": "o", "à": "a", "õ": "o"
    }
    for old, new in replacements.items():
        tag_slug = tag_slug.replace(old, new)
    
    # Verificar no cache primeiro
    if tag_slug in _cache["existing_tags"]:
        return _cache["existing_tags"][tag_slug]["id"]
    
    # Se não está no cache, criar nova tag
    tag_data = {
        "name": tag_name,  # Apenas o nome, sem a categoria
        "slug": tag_slug,  # Slug mantém categoria para evitar conflitos
        "description": tag_description if tag_description else f"Categoria: {tag_category}"
    }
    
    try:
        r = requests.post(f"{NETBOX_URL}/api/extras/tags/", headers=HEADERS, data=json.dumps(tag_data), verify=False, timeout=30)
        if r.status_code in [200, 201]:
            created_tag = r.json()
            tag_id = created_tag["id"]
            # Adicionar ao cache
            _cache["existing_tags"][tag_slug] = created_tag
            print_flush(f"   🏷️ Tag criada: {tag_name}")
            return tag_id
        else:
            print_flush(f"❌ Falha ao criar tag {tag_name}: {r.text}")
            return None
    except Exception as e:
        print_flush(f"❌ Erro ao criar tag {tag_name}: {e}")
        return None

def ensure_vm(vm):
    name = vm.get("vm_name")

    site_id = get_id_by_name("dcim/sites", FORCE_SITE)
    cluster_id = get_id_by_name("virtualization/clusters", vm.get("vm_cluster"))

    # Determinar status baseado no power state
    vm_power_state = vm.get("vm_power_state", "")
    status = "offline" if vm_power_state == "poweredOff" else "active"

    payload = {
        "name": name,
        "vcpus": vm.get("vm_cpu_count"),
        "memory": int(vm.get("vm_memory_mb")),
        "disk": int(vm.get("vm_disk_total_gb") * 1024),
        "status": status,
        "site": site_id,
        "cluster": cluster_id,
        "comments": f"Atualizado via AWX em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    }

    # Usar cache de VMs existentes
    existing_vm = _cache["existing_vms"].get(name)
    if existing_vm:
        vm_id = existing_vm["id"]
        requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, data=json.dumps(payload), verify=False)
        print_flush(f"♻️ VM atualizada: {name}")
    else:
        r = requests.post(f"{NETBOX_URL}/api/virtualization/virtual-machines/", headers=HEADERS, data=json.dumps(payload), verify=False)
        if r.status_code in [200, 201]:
            vm_id = r.json()["id"]
            # Adicionar ao cache
            _cache["existing_vms"][name] = {"id": vm_id, "name": name}
            print_flush(f"✅ VM criada: {name}")
        else:
            print_flush(f"❌ Falha ao criar VM {name}: {r.text}")
            return None

    return vm_id

def update_vm_tags(vm_id, tag_ids):
    """Atualizar tags de uma VM preservando tags existentes"""
    try:
        # Buscar VM para obter tags atuais
        r = requests.get(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, verify=False, timeout=30)
        if r.status_code == 200:
            vm_data = r.json()
            existing_tag_ids = [tag["id"] for tag in vm_data.get("tags", [])]
            
            # Combinar tags existentes com as novas
            all_tag_ids = list(set(existing_tag_ids + tag_ids))
            
            # Atualizar VM com todas as tags
            update_data = {"tags": all_tag_ids}
            
            r_update = requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", 
                                    headers=HEADERS, data=json.dumps(update_data), verify=False, timeout=30)
            
            if r_update.status_code == 200:
                new_tags_count = len(tag_ids) - len(set(tag_ids).intersection(set(existing_tag_ids)))
                if new_tags_count > 0:
                    print_flush(f"   🏷️ {new_tags_count} novas tags adicionadas à VM")
                return True
        return False
    except Exception as e:
        print_flush(f"❌ Erro ao atualizar tags da VM: {e}")
        return False

def ensure_interface(vm_id, name):
    """Criar/verificar interface usando cache para performance"""
    # Usar cache de interfaces existentes
    interface_key = f"{vm_id}_{name}"
    if interface_key in _cache["existing_interfaces"]:
        return _cache["existing_interfaces"][interface_key]["id"]

    # Se não existe no cache, criar nova interface
    payload = {
        "name": name,
        "virtual_machine": vm_id,
        "type": INTERFACE_TYPE
    }
    try:
        r = requests.post(f"{NETBOX_URL}/api/virtualization/interfaces/", headers=HEADERS, data=json.dumps(payload), verify=False, timeout=30)
        if r.status_code in [200, 201]:
            interface_data = r.json()
            interface_id = interface_data["id"]
            # Adicionar ao cache
            _cache["existing_interfaces"][interface_key] = interface_data
            return interface_id
        else:
            print_flush(f"❌ Falha ao criar interface {name}: {r.text}")
            return None
    except Exception as e:
        print_flush(f"❌ Erro ao criar interface {name}: {e}")
        return None

def ensure_ip(ip_str, interface_id):
    """Criar/verificar IP usando cache para performance"""
    # Extrair apenas o IP sem máscara para comparação
    ip_only = ip_str.split("/")[0]
    
    # Usar cache de IPs existentes (buscar por IP sem máscara)
    if ip_only in _cache["existing_ips"]:
        ip_data = _cache["existing_ips"][ip_only]
        ip_id = ip_data["id"]
        
        # Verificar se precisa atualizar a associação
        current_interface = ip_data.get("assigned_object_id")
        if current_interface != interface_id:
            try:
                requests.patch(f"{NETBOX_URL}/api/ipam/ip-addresses/{ip_id}/", headers=HEADERS, data=json.dumps({
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": interface_id
                }), verify=False, timeout=30)
                # Atualizar cache
                _cache["existing_ips"][ip_only]["assigned_object_id"] = interface_id
            except Exception as e:
                print_flush(f"❌ Erro ao atualizar associação do IP {ip_str}: {e}")
        
        return ip_id

    # Se não existe no cache, criar novo IP
    payload = {
        "address": ip_str,
        "status": "active",
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": interface_id
    }
    try:
        r = requests.post(f"{NETBOX_URL}/api/ipam/ip-addresses/", headers=HEADERS, data=json.dumps(payload), verify=False, timeout=30)
        if r.status_code in [200, 201]:
            ip_data = r.json()
            ip_id = ip_data["id"]
            # Adicionar ao cache (usar IP sem máscara como chave)
            _cache["existing_ips"][ip_only] = ip_data
            return ip_id
        else:
            print_flush(f"❌ Falha ao criar IP {ip_str}: {r.text}")
            return None
    except Exception as e:
        print_flush(f"❌ Erro ao criar IP {ip_str}: {e}")
        return None

def update_primary_ip(vm_id, ip_id):
    payload = {"primary_ip4": ip_id}
    requests.patch(f"{NETBOX_URL}/api/virtualization/virtual-machines/{vm_id}/", headers=HEADERS, data=json.dumps(payload), verify=False)

# === EXECUÇÃO PRINCIPAL ===
def main():
    print_flush("🚀 Iniciando sincronização AWX → NetBox...")
    collector = SimpleAWXCollector()
    vms = collector.list_hosts()

    # Pré-carregar TODOS os caches para otimizar performance com 10000+ VMs
    print_flush("📋 Carregando dados existentes do NetBox...")
    
    try:
        # 1. Cache de VMs existentes
        print_flush("   🖥️ Carregando VMs...")
        all_vms = paginated_get_all("virtualization/virtual-machines")
        _cache["existing_vms"] = {vm["name"]: vm for vm in all_vms}
        print_flush(f"   └─ Encontradas {len(_cache['existing_vms'])} VMs no NetBox")
    except Exception as e:
        print_flush(f"❌ Erro ao carregar VMs: {e}")
        _cache["existing_vms"] = {}
    
    try:
        # 2. Cache de interfaces existentes (chave: vm_id_interface_name)
        print_flush("   🔌 Carregando interfaces...")
        all_interfaces = paginated_get_all("virtualization/interfaces")
        _cache["existing_interfaces"] = {}
        for interface in all_interfaces:
            vm_id = interface.get("virtual_machine", {}).get("id")
            if vm_id:
                interface_key = f"{vm_id}_{interface['name']}"
                _cache["existing_interfaces"][interface_key] = interface
        print_flush(f"   └─ Encontradas {len(all_interfaces)} interfaces no NetBox")
    except Exception as e:
        print_flush(f"❌ Erro ao carregar interfaces: {e}")
        _cache["existing_interfaces"] = {}
    
    try:
        # 3. Cache de IPs existentes (chave: endereço_ip)
        print_flush("   🌐 Carregando IPs...")
        all_ips = paginated_get_all("ipam/ip-addresses")
        _cache["existing_ips"] = {}
        for ip in all_ips:
            ip_address = ip.get("address", "").split("/")[0]  # Remove máscara para comparação
            if ip_address:
                _cache["existing_ips"][ip_address] = ip
        print_flush(f"   └─ Encontrados {len(all_ips)} IPs no NetBox")
    except Exception as e:
        print_flush(f"❌ Erro ao carregar IPs: {e}")
        _cache["existing_ips"] = {}
    
    try:
        # 4. Cache de tags existentes (chave: slug)
        print_flush("   🏷️  Carregando tags...")
        all_tags = paginated_get_all("extras/tags")
        _cache["existing_tags"] = {tag["slug"]: tag for tag in all_tags}
        print_flush(f"   └─ Encontradas {len(all_tags)} tags no NetBox")
    except Exception as e:
        print_flush(f"❌ Erro ao carregar tags: {e}")
        _cache["existing_tags"] = {}
    
    print_flush(f"✅ Cache completo carregado!")

    print_flush(f"🔄 Processando {len(vms)} VMs completas (VM + Interface + IP + Tags)...")
    success_count = 0
    error_count = 0
    
    # Processar em lotes para melhor controle
    batch_size = 10
    
    for i, vm in enumerate(vms, 1):
        try:
            vm_name = vm.get("vm_name")
            if not vm_name:
                continue

            # Mostrar progresso mais frequente para grandes volumes
            if i % batch_size == 0 or i == 1 or i == len(vms):
                print_flush(f"📝 Progresso: {i}/{len(vms)} VMs ({success_count} ok, {error_count} erros)")
                # Pequena pausa a cada lote para evitar sobrecarga
                if i % (batch_size * 10) == 0:
                    import time
                    time.sleep(1)
            
            # 1. Criar/atualizar VM
            vm_id = ensure_vm(vm)
            if not vm_id:
                error_count += 1
                continue

            # 2. Processar tags da VM
            vm_tags = vm.get("vm_tags", [])
            if vm_tags:
                tag_ids = []
                for tag in vm_tags:
                    tag_name = tag.get("name", "")
                    tag_category = tag.get("category", "")
                    tag_description = tag.get("description", "")
                    
                    if tag_name and tag_category:
                        tag_id = ensure_tag(tag_name, tag_category, tag_description)
                        if tag_id:
                            tag_ids.append(tag_id)
                
                if tag_ids:
                    update_vm_tags(vm_id, tag_ids)

            # 3. Criar/atualizar interface eth0
            interface_id = ensure_interface(vm_id, "eth0")
            if not interface_id:
                print_flush(f"⚠️ Falha ao criar interface para VM {vm_name}")
                error_count += 1
                continue

            # 4. Processar IPs da VM
            vm_ips = vm.get("vm_ip_addresses", [])
            primary_ip_id = None
            
            if vm_ips:
                # Usar o primeiro IP como primário
                primary_ip = vm_ips[0]
                if primary_ip and primary_ip != "":
                    # Adicionar /32 se não tiver máscara
                    if "/" not in primary_ip:
                        primary_ip = f"{primary_ip}/32"
                    
                    primary_ip_id = ensure_ip(primary_ip, interface_id)
                    if primary_ip_id:
                        print_flush(f"✅ IP {primary_ip} associado à interface eth0 da VM {vm_name}")
                    else:
                        print_flush(f"⚠️ Falha ao associar IP {primary_ip} à VM {vm_name}")

            # 5. Definir IP primário na VM
            if primary_ip_id:
                update_primary_ip(vm_id, primary_ip_id)
                print_flush(f"🎯 IP primário definido para VM {vm_name}")

            success_count += 1

        except KeyboardInterrupt:
            print_flush(f"\n⚠️ Interrompido pelo usuário. Processadas {i} VMs.")
            break
        except Exception as e:
            error_count += 1
            print_flush(f"❌ Erro ao processar VM {vm.get('vm_name')}: {e}")
            # Continuar com a próxima VM mesmo em caso de erro
            
    print_flush(f"🎉 Sincronização concluída! ✅ {success_count} VMs processadas, ❌ {error_count} erros")

if __name__ == "__main__":
    main()