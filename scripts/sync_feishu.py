#!/usr/bin/env python3
"""
飞书多维表格 -> graph_data.json 同步脚本
由 GitHub Actions 定时调用，输出格式与 ECharts HTML 完全兼容
"""
import os, json, random, time, sys, urllib.request, urllib.error

APP_ID = os.environ.get('FEISHU_APP_ID', '')
APP_SECRET = os.environ.get('FEISHU_APP_SECRET', '')
BASE_TOKEN = os.environ.get('FEISHU_BASE_TOKEN', '')

TABLES = {
    'core': 'tblWkWUNC0skgj2W',
    'potential': 'tblstZFfpLWf7xjG',
}

RELATION_FIELDS = [
    {'field_name': '强相关同事(关联)', 'edge_type': 'strong_colleague'},
    {'field_name': '弱相关同事(关联)', 'edge_type': 'weak_colleague'},
    {'field_name': '同学(关联)', 'edge_type': 'classmate'},
]

MAX_SHARED_PER_GROUP = 30
FEISHU_API = 'https://open.feishu.cn'


def api_request(url, method='GET', data=None, headers=None):
    headers = headers or {}
    if data:
        data = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def get_tenant_token():
    url = f'{FEISHU_API}/open-apis/auth/v3/tenant_access_token/internal'
    result = api_request(url, 'POST', {'app_id': APP_ID, 'app_secret': APP_SECRET})
    if result.get('code') != 0:
        raise Exception(f'Token error: {result.get("msg")}')
    return result['tenant_access_token']


def fetch_all_records(token, table_id):
    all_records = []
    page_token = None
    while True:
        params = {'page_size': '500'}
        if page_token:
            params['page_token'] = page_token
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        url = f'{FEISHU_API}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records?{qs}'
        result = api_request(url, headers={'Authorization': f'Bearer {token}'})
        if result.get('code') != 0:
            raise Exception(f'Fetch error [{table_id}]: {result.get("msg")}')
        items = result.get('data', {}).get('items', [])
        all_records.extend(items)
        if not result.get('data', {}).get('has_more', False):
            break
        page_token = result['data'].get('page_token')
    return all_records


def safe_str(val):
    if val is None:
        return ''
    if isinstance(val, list):
        return '、'.join(str(x) for x in val if x)
    if isinstance(val, dict):
        return str(val.get('text', ''))
    return str(val)


def build_graph_data():
    print('[1/4] 获取 tenant_access_token...')
    token = get_tenant_token()

    print('[2/4] 拉取核心人才表...')
    core_records = fetch_all_records(token, TABLES['core'])
    print(f'  -> {len(core_records)} 条记录')

    print('[3/4] 拉取潜在人才表...')
    potential_records = fetch_all_records(token, TABLES['potential'])
    print(f'  -> {len(potential_records)} 条记录')

    print('[4/4] 构建图数据...')
    nodes = {}
    edges = []
    company_groups = {}
    school_groups = {}

    def add_node(fields, source):
        name = safe_str(fields.get('姓名', '')).strip()
        if not name or name in nodes:
            return
        company = safe_str(fields.get('公司', ''))
        school = safe_str(fields.get('学校', ''))
        skills = safe_str(fields.get('技能标签', ''))
        position = safe_str(fields.get('职位', ''))
        education = safe_str(fields.get('学历', ''))

        nodes[name] = {
            'name': name,
            'company': company,
            'title': position,
            'tags': skills,
            'edu': education,
            'school': school,
            'source': source,
            'record_id': fields.get('record_id', ''),
        }
        if company:
            company_groups.setdefault(company, []).append(name)
        if school:
            school_groups.setdefault(school, []).append(name)

    for rec in core_records:
        f = rec.get('fields', {})
        f['record_id'] = rec.get('record_id', '')
        add_node(f, '核心人才')
    for rec in potential_records:
        f = rec.get('fields', {})
        f['record_id'] = rec.get('record_id', '')
        add_node(f, '潜在人才')

    # 关系边（核心人才表）
    for rec in core_records:
        fields = rec.get('fields', {})
        name = safe_str(fields.get('姓名', '')).strip()
        if not name:
            continue
        for rf in RELATION_FIELDS:
            val = fields.get(rf['field_name'])
            if isinstance(val, dict):
                text_arr = val.get('text_arr', [])
            elif isinstance(val, list):
                text_arr = [str(x).strip() for x in val if x]
            else:
                text_arr = []
            for target in text_arr:
                target = str(target).strip()
                if target and target != name and target in nodes:
                    edges.append({'source': name, 'target': target, 'type': rf['edge_type']})

    # 同公司/同学校边（采样限制）
    def add_shared_edges(group_map, edge_type):
        for key, names in group_map.items():
            if len(names) < 2:
                continue
            sampled = random.sample(names, min(len(names), MAX_SHARED_PER_GROUP))
            for i in range(len(sampled)):
                for j in range(i + 1, len(sampled)):
                    edges.append({'source': sampled[i], 'target': sampled[j], 'type': edge_type, 'label': key})

    add_shared_edges(company_groups, 'shared_company')
    add_shared_edges(school_groups, 'shared_school')

    return {
        'nodes': nodes,
        'edges': edges,
        'count': len(nodes),
        'edge_count': len(edges),
        'synced_at': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)),
        'timestamp': int(time.time() * 1000),
    }


if __name__ == '__main__':
    random.seed(42)
    graph = build_graph_data()
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'graph_data.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(graph, f, ensure_ascii=False)
    print(f'Done! {graph["count"]} nodes, {graph["edge_count"]} edges -> graph_data.json')
    print(f'Synced at: {graph["synced_at"]}')
