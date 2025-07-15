#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor Simples AWX - Coleta dados via API sem Ansible
Versão simplificada para testes rápidos
"""

import requests
import json
import os
from urllib3.exceptions import InsecureRequestWarning

# Desabilitar warnings SSL
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

class SimpleAWXCollector:
    def __init__(self, awx_url, awx_user, awx_password):
        self.awx_url = awx_url.rstrip('/')
        self.awx_user = awx_user
        self.awx_password = awx_password
        self.session = requests.Session()
        self.session.auth = (awx_user, awx_password)
        self.session.verify = False
        
        # Testar conectividade
        self.test_connection()
    
    def test_connection(self):
        """Testa a conectividade com o AWX"""
        try:
            print("🔍 Testando conectividade com AWX...")
            url = f"{self.awx_url}/api/v2/"
            response = self.session.get(url)
            response.raise_for_status()
            
            user_info = response.json().get('current_user', {})
            print(f"✅ Conectado como: {user_info.get('username', 'Desconhecido')}")
            return True
        except Exception as e:
            print(f"❌ Erro de conectividade: {e}")
            return False
    
    def list_inventories(self):
        """Lista todos os inventários"""
        try:
            print("\n📦 Buscando inventários...")
            url = f"{self.awx_url}/api/v2/inventories/"
            response = self.session.get(url)
            response.raise_for_status()
            
            data = response.json()
            print(f"📋 Encontrados {data['count']} inventário(s):")
            
            inventories = []
            for inv in data['results']:
                print(f"   - {inv['name']} (ID: {inv['id']}) - {inv.get('description', 'Sem descrição')}")
                inventories.append({
                    'id': inv['id'],
                    'name': inv['name'],
                    'description': inv.get('description', ''),
                    'hosts_count': inv.get('hosts_with_active_failures', 0)
                })
            
            return inventories
        except Exception as e:
            print(f"❌ Erro ao buscar inventários: {e}")
            return []
    
    def get_inventory_hosts(self, inventory_id, inventory_name):
        """Busca hosts de um inventário específico"""
        try:
            print(f"\n🖥️ Buscando hosts do inventário '{inventory_name}' (ID: {inventory_id})...")
            url = f"{self.awx_url}/api/v2/inventories/{inventory_id}/hosts/"
            response = self.session.get(url)
            response.raise_for_status()
            
            data = response.json()
            hosts = data['results']
            print(f"📊 Encontrados {len(hosts)} host(s):")
            
            host_list = []
            for host in hosts:
                status = "🟢" if host['enabled'] else "🔴"
                print(f"   {status} {host['name']} (ID: {host['id']})")
                
                # Buscar detalhes do host
                host_details = self.get_host_details(host['id'])
                if host_details:
                    host_list.append(host_details)
            
            return host_list
        except Exception as e:
            print(f"❌ Erro ao buscar hosts do inventário {inventory_id}: {e}")
            return []
    
    def get_host_details(self, host_id):
        """Busca detalhes completos de um host"""
        try:
            url = f"{self.awx_url}/api/v2/hosts/{host_id}/"
            response = self.session.get(url)
            response.raise_for_status()
            
            host_data = response.json()
            
            # Parse das variáveis
            variables = {}
            if host_data.get('variables'):
                try:
                    variables = json.loads(host_data['variables'])
                except:
                    variables = {}
            
            # Buscar grupos
            groups = self.get_host_groups(host_id)
            
            host_info = {
                'id': host_data['id'],
                'name': host_data['name'],
                'description': host_data.get('description', ''),
                'enabled': host_data['enabled'],
                'variables': variables,
                'groups': groups,
                # Extrair informações úteis das variáveis
                'ansible_host': variables.get('ansible_host', ''),
                'vm_name': variables.get('vm_name', ''),
                'vm_guest_os': variables.get('vm_guest_os', ''),
                'vm_power_state': variables.get('vm_power_state', ''),
                'vm_cpu_count': variables.get('vm_cpu_count', ''),
                'vm_memory_gb': variables.get('vm_memory_gb', ''),
                'vm_datacenter': variables.get('vm_datacenter', ''),
                'vm_cluster': variables.get('vm_cluster', ''),
                'vm_uuid': variables.get('vm_uuid', ''),
                'vm_ip_addresses': variables.get('vm_ip_addresses', [])
            }
            
            return host_info
        except Exception as e:
            print(f"❌ Erro ao buscar detalhes do host {host_id}: {e}")
            return None
    
    def get_host_groups(self, host_id):
        """Busca grupos de um host"""
        try:
            url = f"{self.awx_url}/api/v2/hosts/{host_id}/groups/"
            response = self.session.get(url)
            response.raise_for_status()
            
            data = response.json()
            return [group['name'] for group in data['results']]
        except Exception as e:
            print(f"❌ Erro ao buscar grupos do host {host_id}: {e}")
            return []
    
    def display_host_details(self, host):
        """Exibe detalhes formatados de um host"""
        print(f"\n" + "="*60)
        print(f"🖥️ Host: {host['name']} (ID: {host['id']})")
        print(f"📝 Descrição: {host['description']}")
        print(f"🔛 Habilitado: {'Sim' if host['enabled'] else 'Não'}")
        print(f"📡 IP Ansible: {host['ansible_host']}")
        
        if host['vm_name']:
            print(f"\n🖼️ Informações da VM:")
            print(f"   Nome da VM: {host['vm_name']}")
            print(f"   Sistema Operacional: {host['vm_guest_os']}")
            print(f"   Estado: {host['vm_power_state']}")
            print(f"   CPU: {host['vm_cpu_count']}")
            print(f"   Memória: {host['vm_memory_gb']} GB")
            print(f"   Datacenter: {host['vm_datacenter']}")
            print(f"   Cluster: {host['vm_cluster']}")
            print(f"   UUID: {host['vm_uuid']}")
            if host['vm_ip_addresses']:
                print(f"   IPs: {', '.join(host['vm_ip_addresses'])}")
        
        if host['groups']:
            print(f"\n👥 Grupos: {', '.join(host['groups'])}")
        
        print(f"\n🔧 Comando para detalhes via API:")
        print(f"curl -u '{self.awx_user}:****' '{self.awx_url}/api/v2/hosts/{host['id']}/' | jq '.'")

def main():
    # Configurações
    AWX_URL = os.getenv('AWX_URL', 'http://10.0.100.159:8013')
    AWX_USER = os.getenv('AWX_USER', 'junior')
    AWX_PASSWORD = os.getenv('AWX_PASSWORD', 'JR83silV@83')
    
    # Filtros opcionais
    INVENTORY_FILTER = os.getenv('INVENTORY_FILTER', 'VMware Inventory')  # Nome do inventário
    HOST_FILTER = os.getenv('HOST_FILTER', '')  # Filtro por nome do host
    
    print("🚀 Coletor Simples AWX - Iniciando...")
    print(f"🔗 AWX URL: {AWX_URL}")
    print(f"👤 Usuário: {AWX_USER}")
    print(f"📦 Filtro de inventário: {INVENTORY_FILTER}")
    print(f"🖥️ Filtro de host: {HOST_FILTER if HOST_FILTER else 'Nenhum'}")
    
    # Inicializar coletor
    collector = SimpleAWXCollector(AWX_URL, AWX_USER, AWX_PASSWORD)
    
    # Listar inventários
    inventories = collector.list_inventories()
    if not inventories:
        print("❌ Nenhum inventário encontrado")
        return
    
    # Filtrar inventários
    target_inventories = []
    if INVENTORY_FILTER:
        for inv in inventories:
            if INVENTORY_FILTER.lower() in inv['name'].lower():
                target_inventories.append(inv)
    else:
        target_inventories = inventories
    
    if not target_inventories:
        print(f"❌ Nenhum inventário encontrado com o filtro: {INVENTORY_FILTER}")
        return
    
    print(f"\n🎯 Processando {len(target_inventories)} inventário(s):")
    for inv in target_inventories:
        print(f"   - {inv['name']}")
    
    # Coletar hosts
    all_hosts = []
    for inventory in target_inventories:
        hosts = collector.get_inventory_hosts(inventory['id'], inventory['name'])
        all_hosts.extend(hosts)
    
    # Aplicar filtro de host
    if HOST_FILTER:
        filtered_hosts = [h for h in all_hosts if HOST_FILTER.lower() in h['name'].lower()]
    else:
        filtered_hosts = all_hosts
    
    print(f"\n📊 Resumo:")
    print(f"   Total de hosts encontrados: {len(all_hosts)}")
    print(f"   Hosts após filtro: {len(filtered_hosts)}")
    
    # Exibir detalhes dos hosts
    for host in filtered_hosts:
        collector.display_host_details(host)
    
    # Salvar dados em JSON (opcional)
    output_file = 'awx_hosts_data.json'
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(filtered_hosts, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Dados salvos em: {output_file}")
    except Exception as e:
        print(f"❌ Erro ao salvar arquivo: {e}")
    
    print(f"\n✅ Coleta concluída! {len(filtered_hosts)} host(s) processado(s).")

if __name__ == "__main__":
    main()
