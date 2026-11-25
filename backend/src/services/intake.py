"""两班分开交表：收件箱、到齐装配与不可变版本。

约定：

* ``baseline`` 是原垫班测量表，``trial`` 是试装垫班测量表；
* 同一 ``batch_code`` 下两张表都到齐，才创建 ``data_revisions`` 可分析版本；
* 每次装配都按两张原表的完整内容计算指纹，迟到补测会形成新的不可变版本，
  旧版本与其分析结果不会被改写，重跑旧版本判定不变。
"""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models.database import add_event, engine
from src.models.tables import dataset_pairings, intake_submissions
from src.services.data import save_revision
from src.services.pairing import normalize_measurements, pair_windows

DEFAULT_BATCH = 'PAD-SHIFT-CURRENT'
KINDS = ('baseline', 'trial')
KIND_LABEL = {'baseline': '原垫班', 'trial': '试装垫班'}


def _checksum(records) -> str:
    payload = json.dumps(records, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def store_shift_table(batch_code, kind, filename, records, owner):
    """保存一班的测量表。相同 batch/kind/内容重复提交时幂等返回。"""
    if kind not in KINDS:
        raise ValueError('交表种类只能是 baseline（原垫班）或 trial（试装垫班）。')
    batch_code = (batch_code or DEFAULT_BATCH).strip() or DEFAULT_BATCH
    filename = (filename or f'{kind}.csv').strip()[:180]
    checksum = _checksum(records)
    now = time.time()
    try:
        with engine.begin() as connection:
            existing = connection.execute(
                select(intake_submissions.c.id).where(
                    intake_submissions.c.batch_code == batch_code,
                    intake_submissions.c.kind == kind,
                    intake_submissions.c.checksum == checksum,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {'id': existing, 'created': False, 'batch_code': batch_code, 'kind': kind}
            key = connection.execute(intake_submissions.insert().values(
                batch_code=batch_code, kind=kind, filename=filename, checksum=checksum,
                records=records, rows_count=len(records), owner=owner, created=now,
            )).inserted_primary_key[0]
            add_event(connection, owner, 'intake', key, now)
            return {'id': key, 'created': True, 'batch_code': batch_code, 'kind': kind}
    except IntegrityError:
        with engine.connect() as connection:
            existing = connection.execute(
                select(intake_submissions.c.id).where(
                    intake_submissions.c.batch_code == batch_code,
                    intake_submissions.c.kind == kind,
                    intake_submissions.c.checksum == checksum,
                )
            ).scalar_one_or_none()
        if existing is None:
            raise
        return {'id': existing, 'created': False, 'batch_code': batch_code, 'kind': kind}


def _latest_table(connection, batch_code, kind):
    row = connection.execute(
        select(intake_submissions).where(
            intake_submissions.c.batch_code == batch_code,
            intake_submissions.c.kind == kind,
        ).order_by(intake_submissions.c.id.desc()).limit(1)
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def save_pairing_dataset(name, baseline_records, trial_records, owner, batch_code=None):
    """两张表到齐后配对并写入不可变数据版本，同时存档两套配对。"""
    pairing = pair_windows(baseline_records, trial_records)
    batch_code = batch_code or DEFAULT_BATCH
    full_fingerprint = {
        'batch_code': batch_code,
        'baseline': baseline_records,
        'trial': trial_records,
        'pairing_schema': 'overlap-v1',
    }
    key, created = save_revision(name, pairing['plan_a'], owner, fingerprint=full_fingerprint)
    # 配对存档只在首次创建版本时写入；已存在的版本绝不覆盖，保证重跑口径不变。
    # 并发装配同一指纹时另一事务可能已写入，主键冲突直接忽略。
    try:
        with engine.begin() as connection:
            exists = connection.scalar(
                select(dataset_pairings.c.dataset_id).where(dataset_pairings.c.dataset_id == key)
            )
            if exists is None:
                connection.execute(dataset_pairings.insert().values(dataset_id=key, payload=pairing))
                add_event(connection, owner, 'pair', key, time.time())
    except IntegrityError:
        pass
    return key, created, pairing


def assemble_batch(batch_code, owner):
    """若同一交接批两张表都到齐，就装配出可分析版本；否则返回缺表状态。"""
    batch_code = (batch_code or DEFAULT_BATCH).strip() or DEFAULT_BATCH
    with engine.connect() as connection:
        baseline = _latest_table(connection, batch_code, 'baseline')
        trial = _latest_table(connection, batch_code, 'trial')
    received = [kind for kind in KINDS if (baseline if kind == 'baseline' else trial) is not None]
    if len(received) < 2:
        return {
            'batch_code': batch_code,
            'complete': False,
            'received': received,
            'waiting_for': [KIND_LABEL[kind] for kind in KINDS if kind not in received],
            'dataset_id': None,
        }
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = f'两班配对版本 {batch_code} {stamp}'
    key, created, pairing = save_pairing_dataset(
        name, baseline['records'], trial['records'], owner, batch_code)
    return {
        'batch_code': batch_code,
        'complete': True,
        'received': list(KINDS),
        'dataset_id': key,
        'created': created,
        'paired_station_count': pairing['paired_station_count'],
        'tie_count': pairing['tie_count'],
        'tie_stations': pairing['tie_stations'],
        'unpaired_stations': pairing['unpaired_stations'],
    }


def submit_and_maybe_assemble(batch_code, kind, filename, records, owner):
    # 先做业务校验（站号、窗口、热损），不合法直接 422，且不把坏表写进收件箱。
    normalize_measurements(records, kind)
    stored = store_shift_table(batch_code, kind, filename, records, owner)
    assembly = assemble_batch(stored['batch_code'], owner)
    return {**stored, 'assembly': assembly}


def list_intake(limit=100):
    """按交接批汇总两班交表状态，供数据台账页展示。"""
    with engine.connect() as connection:
        rows = connection.execute(
            select(intake_submissions).order_by(intake_submissions.c.id.desc()).limit(500)
        ).mappings().all()
        pairing_rows = connection.execute(
            select(dataset_pairings.c.dataset_id, dataset_pairings.c.payload)
        ).mappings().all()
    dataset_batch = {}
    for row in pairing_rows:
        payload = row['payload'] or {}
        dataset_batch[row['dataset_id']] = payload.get('batch_code')
    batches = {}
    for row in rows:
        code = row['batch_code']
        batch = batches.setdefault(code, {
            'batch_code': code, 'kinds': {}, 'last_created': row['created'],
        })
        batch['last_created'] = max(batch['last_created'], row['created'])
        current = batch['kinds'].get(row['kind'])
        if current is None or row['id'] > current['id']:
            batch['kinds'][row['kind']] = {
                'id': row['id'], 'filename': row['filename'], 'rows_count': row['rows_count'],
                'owner': row['owner'], 'created': row['created'], 'checksum': row['checksum'][:12],
            }
    result = []
    for code, batch in batches.items():
        kinds = batch['kinds']
        result.append({
            'batch_code': code,
            'baseline': kinds.get('baseline'),
            'trial': kinds.get('trial'),
            'complete': 'baseline' in kinds and 'trial' in kinds,
            'last_created': batch['last_created'],
        })
    result.sort(key=lambda item: item['last_created'], reverse=True)
    return {'batches': result[:limit], 'dataset_batch': dataset_batch}
