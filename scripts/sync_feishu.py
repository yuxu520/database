#!/usr/bin/env python3
"""
飞书多维表格 -> graph_data.js 同步脚本 v4
输出 JS 文件（window.ALL_NODES / window.ALL_EDGES），供新版网络图直接加载
新增：读取主表「推荐人」字段，自动生成 referral 推荐边（线人 -> 被推荐人）
"""
import os, json, random, time, sys, urllib.request, urllib.error
from datetime import datetime

APP_ID = os.environ.get('FEISHU_APP_ID', 'cli_aada7ce7a4e21bee')
APP_SECRET = os.environ.get('FEISHU_APP_SECRET', 'UOTi7iw3kkT2Nowu9LvJ8fvcUa6NY2wc')
BASE_TOKEN = os.environ.get('FEISHU_BASE_TOKEN', 'Teb8bGpkHaU8GrsFArAcPOrxnXd')

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


def ts_to_date_str(ts):
    """将毫秒时间戳转为 'YYYY.MM' 格式字符串"""
    if ts is None:
        return None
    try:
        dt = datetime.utcfromtimestamp(ts / 1000.0)
        return f'{dt.year}.{dt.month:02d}'
    except:
        return None


def calc_overlap(start1, end1, start2, end2):
    """
    计算两个时间段的重叠部分
    返回 (overlap_start_str, overlap_end_str) 或 None
    时间戳为空（None）表示当前在职/在读，用极大值替代
    """
    if start1 is None or start2 is None:
        return None
    
    # None 表示当前，用极大值替代
    e1 = end1 if end1 is not None else 9999999999999
    e2 = end2 if end2 is not None else 9999999999999
    
    overlap_start = max(start1, start2)
    overlap_end = min(e1, e2)
    
    if overlap_start > overlap_end:
        return None
    
    s_str = ts_to_date_str(overlap_start)
    e_str = ts_to_date_str(overlap_end) if overlap_end != 9999999999999 else '至今'
    
    if s_str and e_str:
        return f'{s_str}-{e_str}'
    return None


def fetch_work_experiences(token, table_id):
    """
    拉取工作经历子表，返回 person_work_experiences 字典
    格式: {"人名": [{"company": "xxx", "department": "xxx", "start": ts, "end": ts}, ...]}
    """
    records = fetch_all_records(token, table_id)
    result = {}
    for rec in records:
        fields = rec.get('fields', {})
        # 姓名是关联字段 type=18
        name_val = fields.get('姓名')
        names = extract_names_from_relation(name_val)
        if not names:
            continue
        name = names[0].strip()
        if not name:
            continue
        
        company = safe_str(fields.get('公司名称', ''))
        department = safe_str(fields.get('部门', ''))
        start_ts = fields.get('开始日期')
        end_ts = fields.get('结束日期')
        
        # 时间戳可能是毫秒整数
        if isinstance(start_ts, (int, float)) and start_ts > 0:
            start_ts = int(start_ts)
        else:
            start_ts = None
        if isinstance(end_ts, (int, float)) and end_ts > 0:
            end_ts = int(end_ts)
        else:
            end_ts = None
        
        entry = {
            'company': company,
            'department': department,
            'start': start_ts,
            'end': end_ts,
        }
        
        if name not in result:
            result[name] = []
        result[name].append(entry)
    return result


def fetch_edu_experiences(token, table_id):
    """
    拉取教育经历子表，返回 person_edu_experiences 字典
    格式: {"人名": [{"school": "xxx", "major": "xxx", "degree": "xxx", "start": ts, "end": ts}, ...]}
    """
    records = fetch_all_records(token, table_id)
    result = {}
    for rec in records:
        fields = rec.get('fields', {})
        name_val = fields.get('姓名')
        names = extract_names_from_relation(name_val)
        if not names:
            continue
        name = names[0].strip()
        if not name:
            continue
        
        school = safe_str(fields.get('学校名称', ''))
        major = safe_str(fields.get('专业', ''))
        degree = safe_str(fields.get('学历', ''))
        start_ts = fields.get('开始日期')
        end_ts = fields.get('结束日期')
        
        if isinstance(start_ts, (int, float)) and start_ts > 0:
            start_ts = int(start_ts)
        else:
            start_ts = None
        if isinstance(end_ts, (int, float)) and end_ts > 0:
            end_ts = int(end_ts)
        else:
            end_ts = None
        
        entry = {
            'school': school,
            'major': major,
            'degree': degree,
            'start': start_ts,
            'end': end_ts,
        }
        
        if name not in result:
            result[name] = []
        result[name].append(entry)
    return result


def find_work_overlap(person_work_experiences, name1, name2):
    """
    查找两人在工作经历中同一家公司的交叉信息
    返回 (company, department, overlap_str) 或 None
    """
    works1 = person_work_experiences.get(name1, [])
    works2 = person_work_experiences.get(name2, [])
    
    best = None
    for w1 in works1:
        for w2 in works2:
            c1 = w1.get('company', '')
            c2 = w2.get('company', '')
            if c1 and c2 and (c1 == c2 or c1 in c2 or c2 in c1):
                overlap = calc_overlap(w1.get('start'), w1.get('end'), w2.get('start'), w2.get('end'))
                dept = w1.get('department', '') or w2.get('department', '')
                company = c1
                if not best:
                    best = (company, dept, overlap)
                elif overlap and not best[2]:
                    # 优先有overlap的
                    best = (company, dept, overlap)
    return best


def find_edu_overlap(person_edu_experiences, name1, name2):
    """
    查找两人在教育经历中同一所学校的交叉信息
    返回 (school, major, overlap_str) 或 None
    """
    edu1 = person_edu_experiences.get(name1, [])
    edu2 = person_edu_experiences.get(name2, [])
    
    best = None
    for e1 in edu1:
        for e2 in edu2:
            s1 = e1.get('school', '')
            s2 = e2.get('school', '')
            if s1 and s2 and (s1 == s2 or s1 in s2 or s2 in s1):
                overlap = calc_overlap(e1.get('start'), e1.get('end'), e2.get('start'), e2.get('end'))
                major = e1.get('major', '') or e2.get('major', '')
                school = s1
                if not best:
                    best = (school, major, overlap)
                elif overlap and not best[2]:
                    best = (school, major, overlap)
    return best


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
    # 工作经历
    core_work_exp = fetch_work_experiences(token, TABLES['core_work'])
    print(f'  -> 核心人才工作经历: {len(core_work_exp)} 人')
    potential_work_exp = fetch_work_experiences(token, TABLES['potential_work'])
    print(f'  -> 潜在人才工作经历: {len(potential_work_exp)} 人')
    
    # 教育经历
    core_edu_exp = fetch_edu_experiences(token, TABLES['core_edu'])
    print(f'  -> 核心人才教育经历: {len(core_edu_exp)} 人')
    potential_edu_exp = fetch_edu_experiences(token, TABLES['potential_edu'])
    print(f'  -> 潜在人才教育经历: {len(potential_edu_exp)} 人')
    
    # 合并工作经历和教育经历
    person_work_experiences = {}
    person_work_experiences.update(core_work_exp)
    person_work_experiences.update(potential_work_exp)
    
    person_edu_experiences = {}
    person_edu_experiences.update(core_edu_exp)
    person_edu_experiences.update(potential_edu_exp)
    
    print(f'  -> 合计工作经历: {len(person_work_experiences)} 人, 教育经历: {len(person_edu_experiences)} 人')

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

    referrals = []  # (推荐人, 被推荐人)
    for rec in core_records:
        f = rec.get('fields', {})
        f['record_id'] = rec.get('record_id', '')
        add_node(f, '核心人才')
        nm = safe_str(f.get('姓名', '')).strip()
        rf = safe_str(f.get('推荐人', '')).strip()
        if nm and rf:
            referrals.append((rf, nm))
    for rec in potential_records:
        f = rec.get('fields', {})
        f['record_id'] = rec.get('record_id', '')
        add_node(f, '潜在人才')
        nm = safe_str(f.get('姓名', '')).strip()
        rf = safe_str(f.get('推荐人', '')).strip()
        if nm and rf:
            referrals.append((rf, nm))

    # 关系边 - 增强版：附加子表交叉信息
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
                        
                        edge_type = rf['edge_type']
                        if edge_type in ('strong_colleague', 'weak_colleague', 'classmate'):
                            # 尝试查找工作经历交叉
                            if edge_type != 'classmate':
                                work_info = find_work_overlap(person_work_experiences, name, target)
                                if work_info:
                                    company, dept, overlap = work_info
                                    if company:
                                        edge['company'] = company
                                    if dept:
                                        edge['department'] = dept
                                    if overlap:
                                        edge['overlap'] = overlap
                            # 尝试查找教育经历交叉
                            if edge_type == 'classmate':
                                edu_info = find_edu_overlap(person_edu_experiences, name, target)
                                if edu_info:
                                    school, major, overlap = edu_info
                                    if school:
                                        edge['school'] = school
                                    if major:
                                        edge['major'] = major
                                    if overlap:
                                        edge['overlap'] = overlap
                        
                        edges.append(edge)
                        relation_count += 1
    print(f'  -> 关系边: {relation_count}')

    # 同公司/同学校边 - 增强版
    shared_count = 0
    def add_shared_edges(group_map, edge_type, skip_pairs=None):
        nonlocal shared_count
        skip_pairs = skip_pairs or set()
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
                        edge = {
                            'source': sampled[i],
                            'target': sampled[j],
                            'type': edge_type,
                        }
                        
                        if edge_type == 'shared_company':
                            edge['company'] = key
                            # 从子表查找交叉信息
                            work_info = find_work_overlap(person_work_experiences, sampled[i], sampled[j])
                            if work_info:
                                _, dept, overlap = work_info
                                if dept:
                                    edge['department'] = dept
                                if overlap:
                                    edge['overlap'] = overlap
                        elif edge_type == 'shared_school':
                            edge['school'] = key
                            # 从子表查找交叉信息
                            edu_info = find_edu_overlap(person_edu_experiences, sampled[i], sampled[j])
                            if edu_info:
                                _, major, overlap = edu_info
                                if major:
                                    edge['major'] = major
                                if overlap:
                                    edge['overlap'] = overlap
                        
                        edges.append(edge)
                        shared_count += 1

    # 推荐边：主表「推荐人」字段 -> referral 类型边（线人 -> 被推荐人）
    ref_count = 0
    for ref_name, target in referrals:
        if ref_name in nodes and target in nodes and ref_name != target:
            pair = tuple(sorted([ref_name, target]))
            edge_key = pair + ('referral',)
            if edge_key not in edge_set:
                edge_set.add(edge_key)
                edges.append({'source': ref_name, 'target': target, 'type': 'referral', 'detail': '线人推荐'})
                ref_count += 1
    print(f'  -> 推荐边: {ref_count}')

    # 统一边字段：overlap 时间并入 detail/major（对齐前端展示格式）
    for edge in edges:
        ov = edge.pop('overlap', None)
        dept = edge.pop('department', None)
        if ov:
            ov_fmt = '(' + ov.replace('-', '~') + ')'
            if edge.get('major'):
                edge['major'] = edge['major'] + ' ' + ov_fmt
            elif edge.get('detail'):
                edge['detail'] = (dept + ' ' if dept else '') + ov_fmt if dept else ov_fmt
            else:
                edge['detail'] = (dept + ' ' if dept else '') + ov_fmt

    add_shared_edges(company_groups, 'shared_company', relation_pairs)
    add_shared_edges(school_groups, 'shared_school')
    print(f'  -> shared edges: {shared_count}')

    print('[6/6] 完成构建')
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
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'graph_data.js')
    js = 'window.ALL_NODES = ' + json.dumps(graph['nodes'], ensure_ascii=False, separators=(',', ':')) + ';\n'
    js += 'window.ALL_EDGES = ' + json.dumps(graph['edges'], ensure_ascii=False, separators=(',', ':')) + ';\n'
    js += 'window.GRAPH_SYNCED_AT = ' + json.dumps(graph['synced_at'], ensure_ascii=False) + ';\n'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(js)
    print(f'Done! {graph["count"]} nodes, {graph["edge_count"]} edges -> graph_data.js')
    print(f'Synced at: {graph["synced_at"]}')
