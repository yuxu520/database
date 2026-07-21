#!/usr/bin/env python3
"""
飞书多维表格 -> graph_data.json 同步脚本 v2
增强：拉取工作经历/教育经历子表，计算交叉时间段，附加部门/专业信息到边
"""
import os, json, random, time, sys, urllib.request, urllib.error

APP_ID = os.environ.get('FEISHU_APP_ID', '')
APP_SECRET = os.environ.get('FEISHU_APP_SECRET', '')
BASE_TOKEN = os.environ.get('FEISHU_BASE_TOKEN', '')

TABLES = {
    'core': 'tblWkWUNC0skgj2W',
    'potential': 'tblstZFfpLWf7xjG',
    'core_work': 'tblNKB5HaVELdLf9',
    'core_edu': 'tbl5y6MX5Rot4kt3',
    'potential_work': 'tblGQ7uGa8cSPJOd',
    'potential_edu': 'tbljK4RjMd3ZqBvf',
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
        params = 'page_size=500'
        if page_token:
            params += f'&page_token={page_token}'
        url = f'{FEISHU_API}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records?{params}'
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
        parts = []
        for x in val:
            if isinstance(x, dict):
                t = x.get('text', '')
                if t:
                    parts.append(str(t))
            elif x:
                parts.append(str(x))
        return '、'.join(parts)
    if isinstance(val, dict):
        return str(val.get('text', ''))
    return str(val)


def extract_names_from_relation(val):
    """从关系字段中提取人名列表，支持多种飞书返回格式"""
    if val is None:
        return []
    names = []
    if isinstance(val, str):
        for sep in ['、', ',', '，', '\n']:
            val = val.replace(sep, '、')
        return [p.strip() for p in val.split('、') if p.strip()]
    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict):
                text = item.get('text', '')
                if text:
                    for sep in ['、', ',', '，']:
                        text = text.replace(sep, '、')
                    for p in text.split('、'):
                        p = p.strip()
                        if p:
                            names.append(p)
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
        return names
    if isinstance(val, dict):
        text_arr = val.get('text_arr', [])
        if text_arr:
            return [str(t).strip() for t in text_arr if str(t).strip()]
        text = val.get('text', '')
        if text:
            for sep in ['、', ',', '，']:
                text = text.replace(sep, '、')
            return [p.strip() for p in text.split('、') if p.strip()]
        return names
    return []


def split_multi_value(val_str):
    """拆分多值字段（如 '公司A、公司B' -> ['公司A', '公司B']）"""
    if not val_str:
        return []
    for sep in ['、', ',', '，']:
        val_str = val_str.replace(sep, '|')
    return [p.strip() for p in val_str.split('|') if p.strip()]


def ts_to_ym(ts_ms):
    """时间戳毫秒 -> 'YYYY.MM' 格式字符串"""
    if not ts_ms:
        return None
    try:
        dt = time.gmtime(ts_ms / 1000)
        return f'{dt.tm_year}.{dt.tm_mon:02d}'
    except Exception:
        return None


def calc_overlap_period(start1, end1, start2, end2):
    """计算两个时间段的重叠部分，返回 'YYYY.MM-YYYY.MM' 或 None"""
    # end 为 None 表示当前在职，用极大值替代
    BIG = 9999999999999
    s1, e1 = start1, end1 or BIG
    s2, e2 = start2, end2 or BIG
    overlap_start = max(s1, s2)
    overlap_end = min(e1, e2)
    if overlap_start > overlap_end:
        return None
    # 格式化显示
    start_str = ts_to_ym(overlap_start)
    if end1 is None and end2 is None:
        end_str = '至今'
    else:
        actual_end = min(e1, e2)
        if actual_end >= BIG:
            end_str = '至今'
        else:
            end_str = ts_to_ym(actual_end)
    if not start_str or not end_str:
        return None
    return f'{start_str}-{end_str}'


def fetch_work_experiences(token, table_ids):
    """拉取工作经历子表，返回 {人名: [{company, department, start, end}, ...]}"""
    person_work = {}
    for table_id in table_ids:
        try:
            records = fetch_all_records(token, table_id)
            for rec in records:
                fields = rec.get('fields', {})
                name_val = fields.get('姓名')
                if not name_val:
                    continue
                # 姓名字段是关联字段，格式为 [{text: "张三", record_ids: [...]}]
                name = ''
                if isinstance(name_val, list) and len(name_val) > 0:
                    name = name_val[0].get('text', '').strip()
                elif isinstance(name_val, str):
                    name = name_val.strip()
                if not name:
                    continue
                company = safe_str(fields.get('公司名称', '')).strip()
                department = safe_str(fields.get('部门', '')).strip()
                start = fields.get('开始日期')  # 时间戳毫秒
                end = fields.get('结束日期')    # 时间戳毫秒，可能为空
                # 检查是否当前在职
                is_current = fields.get('是否当前')
                if isinstance(is_current, list) and len(is_current) > 0:
                    curr_text = is_current[0].get('text', '')
                    if curr_text == '当前':
                        end = None
                if company:
                    person_work.setdefault(name, []).append({
                        'company': company,
                        'department': department,
                        'start': start,
                        'end': end,
                    })
        except Exception as e:
            print(f'  [WARN] 拉取工作经历表 {table_id} 失败: {e}')
    return person_work


def fetch_edu_experiences(token, table_ids):
    """拉取教育经历子表，返回 {人名: [{school, major, degree, start, end}, ...]}"""
    person_edu = {}
    for table_id in table_ids:
        try:
            records = fetch_all_records(token, table_id)
            for rec in records:
                fields = rec.get('fields', {})
                name_val = fields.get('姓名')
                if not name_val:
                    continue
                name = ''
                if isinstance(name_val, list) and len(name_val) > 0:
                    name = name_val[0].get('text', '').strip()
                elif isinstance(name_val, str):
                    name = name_val.strip()
                if not name:
                    continue
                school = safe_str(fields.get('学校名称', '')).strip()
                major = safe_str(fields.get('专业', '')).strip()
                degree = safe_str(fields.get('学历', '')).strip()
                start = fields.get('开始日期')
                end = fields.get('结束日期')
                if school:
                    person_edu.setdefault(name, []).append({
                        'school': school,
                        'major': major,
                        'degree': degree,
                        'start': start,
                        'end': end,
                    })
        except Exception as e:
            print(f'  [WARN] 拉取教育经历表 {table_id} 失败: {e}')
    return person_edu


def find_work_overlap(person_work, name1, name2):
    """查找两人同一家公司的交叉信息，返回 {company, department, overlap} 或 None"""
    work1 = person_work.get(name1, [])
    work2 = person_work.get(name2, [])
    for w1 in work1:
        for w2 in work2:
            if w1['company'] == w2['company']:
                overlap = calc_overlap_period(w1['start'], w1['end'], w2['start'], w2['end'])
                # 部门取两者的第一个非空值
                dept = w1['department'] or w2['department'] or ''
                return {
                    'company': w1['company'],
                    'department': dept,
                    'overlap': overlap or '',
                }
    return None


def find_edu_overlap(person_edu, name1, name2):
    """查找两人同一学校的交叉信息，返回 {school, major, overlap} 或 None"""
    edu1 = person_edu.get(name1, [])
    edu2 = person_edu.get(name2, [])
    for e1 in edu1:
        for e2 in edu2:
            if e1['school'] == e2['school']:
                overlap = calc_overlap_period(e1['start'], e1['end'], e2['start'], e2['end'])
                major = e1['major'] or e2['major'] or ''
                return {
                    'school': e1['school'],
                    'major': major,
                    'overlap': overlap or '',
                }
    return None


def build_graph_data():
    print('[1/6] 获取 tenant_access_token...')
    token = get_tenant_token()

    print('[2/6] 拉取核心人才表...')
    core_records = fetch_all_records(token, TABLES['core'])
    print(f'  -> {len(core_records)} 条记录')

    print('[3/6] 拉取潜在人才表...')
    potential_records = fetch_all_records(token, TABLES['potential'])
    print(f'  -> {len(potential_records)} 条记录')

    print('[4/6] 拉取工作经历/教育经历子表...')
    person_work = fetch_work_experiences(token, [TABLES['core_work'], TABLES['potential_work']])
    print(f'  -> {len(person_work)} 人有工作经历')
    person_edu = fetch_edu_experiences(token, [TABLES['core_edu'], TABLES['potential_edu']])
    print(f'  -> {len(person_edu)} 人有教育经历')

    print('[5/6] 构建图数据...')
    nodes = {}
    edges = []
    company_groups = {}
    school_groups = {}
    edge_set = set()
    relation_pairs = set()

    def add_node(fields, source):
        name = safe_str(fields.get('姓名', '')).strip()
        if not name or name in nodes:
            return
        company = safe_str(fields.get('核心公司', ''))
        school = safe_str(fields.get('学校', ''))
        skills = safe_str(fields.get('技能标签', ''))
        position = safe_str(fields.get('当前职位', ''))
        education = safe_str(fields.get('最高学历', ''))
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
        for c in split_multi_value(company):
            company_groups.setdefault(c, []).append(name)
        for s in split_multi_value(school):
            school_groups.setdefault(s, []).append(name)

    for rec in core_records:
        f = rec.get('fields', {})
        f['record_id'] = rec.get('record_id', '')
        add_node(f, '核心人才')
    for rec in potential_records:
        f = rec.get('fields', {})
        f['record_id'] = rec.get('record_id', '')
        add_node(f, '潜在人才')

    # 关系边（强相关/弱相关/同学）- 附加交叉信息
    relation_count = 0
    for rec in core_records:
        fields = rec.get('fields', {})
        name = safe_str(fields.get('姓名', '')).strip()
        if not name:
            continue
        for rf in RELATION_FIELDS:
            val = fields.get(rf['field_name'])
            targets = extract_names_from_relation(val)
            for target in targets:
                target = target.strip()
                if target and target != name and target in nodes:
                    pair = tuple(sorted([name, target]))
                    edge_key = pair + (rf['edge_type'],)
                    if edge_key not in edge_set:
                        edge_set.add(edge_key)
                        relation_pairs.add(pair)
                        edge = {
                            'source': name,
                            'target': target,
                            'type': rf['edge_type'],
                        }
                        # 查找交叉信息
                        if rf['edge_type'] in ('strong_colleague', 'weak_colleague'):
                            info = find_work_overlap(person_work, name, target)
                            if info:
                                edge['company'] = info['company']
                                edge['department'] = info['department']
                                edge['overlap'] = info['overlap']
                        elif rf['edge_type'] == 'classmate':
                            info = find_edu_overlap(person_edu, name, target)
                            if info:
                                edge['school'] = info['school']
                                edge['major'] = info['major']
                                edge['overlap'] = info['overlap']
                        edges.append(edge)
                        relation_count += 1
    print(f'  -> 关系边: {relation_count}')

    # 同公司/同学校边 - 附加交叉信息
    shared_count = 0
    def add_shared_edges(group_map, edge_type, skip_pairs=None):
        nonlocal shared_count
        skip_pairs = skip_pairs or set()
        is_company = (edge_type == 'shared_company')
        for key, names in group_map.items():
            if len(names) < 2:
                continue
            sampled = random.sample(names, min(len(names), MAX_SHARED_PER_GROUP))
            for i in range(len(sampled)):
                for j in range(i + 1, len(sampled)):
                    pair = tuple(sorted([sampled[i], sampled[j]]))
                    if pair in skip_pairs:
                        continue
                    edge_key = pair + (edge_type,)
                    if edge_key not in edge_set:
                        edge_set.add(edge_key)
                        field_val = 'company' if is_company else 'school'
                        edge = {
                            'source': sampled[i],
                            'target': sampled[j],
                            'type': edge_type,
                        }
                        edge[field_val] = key
                        # 查找交叉信息
                        if is_company:
                            info = find_work_overlap(person_work, sampled[i], sampled[j])
                            if info:
                                edge['department'] = info['department']
                                edge['overlap'] = info['overlap']
                        else:
                            info = find_edu_overlap(person_edu, sampled[i], sampled[j])
                            if info:
                                edge['major'] = info['major']
                                edge['overlap'] = info['overlap']
                        edges.append(edge)
                        shared_count += 1

    add_shared_edges(company_groups, 'shared_company', relation_pairs)
    add_shared_edges(school_groups, 'shared_school')
    print(f'  -> shared edges: {shared_count}')

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
