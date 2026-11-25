from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
# 两班分开交表的收件箱：同一 batch_code 下分 baseline/trial 两类，各自只追加不改写。
intake_submissions = Table('shift_intake_submissions', metadata,
                 Column('id', Integer, primary_key=True),
                 Column('batch_code', String(80), nullable=False, index=True),
                 Column('kind', String(16), nullable=False, index=True),
                 Column('filename', String(180), nullable=False),
                 Column('checksum', String(64), nullable=False),
                 Column('records', JSON, nullable=False),
                 Column('rows_count', Integer, nullable=False),
                 Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False),
                 UniqueConstraint('batch_code', 'kind', 'checksum', name='uq_intake_batch_kind_checksum'))
# 可分析版本对应的重叠配对存档（甲、乙两套及每站并列候选），与数据版本一一绑定。
dataset_pairings = Table('dataset_pairings', metadata,
                 Column('dataset_id', Integer, primary_key=True),
                 Column('payload', JSON, nullable=False))
runs = Table('analysis_runs', metadata, Column('id', Integer, primary_key=True),
             Column('dataset_id', Integer, nullable=False, index=True), Column('options', JSON, nullable=False),
             Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
             Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
             Column('result', JSON), Column('error', Text), Column('decision', String(16)),
             Column('reviewer', String(40)), Column('review_note', Text), Column('reviewed', Double))
schedules = Table('report_schedules', metadata, Column('id', Integer, primary_key=True),
                  Column('run_id', Integer, nullable=False), Column('recipient', String(100), nullable=False),
                  Column('cadence', String(16), nullable=False), Column('next_at', Double, nullable=False, index=True),
                  Column('active', Integer, nullable=False), Column('owner', String(40), nullable=False))
deliveries = Table('report_deliveries', metadata, Column('id', Integer, primary_key=True),
                   Column('schedule_id', Integer, nullable=False), Column('run_id', Integer, nullable=False),
                   Column('recipient', String(100), nullable=False), Column('scheduled', Double, nullable=False),
                   Column('delivered', Double, nullable=False), UniqueConstraint('schedule_id', 'scheduled'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))
