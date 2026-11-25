# 002 两班交表与配对存档

本次只新增两张表，`metadata.create_all` 在新旧数据库上都会自动创建缺失表，不需要改写既有 `data_revisions` / `analysis_runs` 结构。

- `shift_intake_submissions`：两班分开交表的不可变收件记录，按 `batch_code + kind + checksum` 幂等。同一 `batch_code` 下 `baseline`、`trial` 两班最新一张表到齐后才装配 `data_revisions` 版本。
- `dataset_pairings`：与 `data_revisions.id` 一对一的重叠配对存档（每站最久重叠候选、甲乙两套配对）。旧数据版本没有对应行时，分析仍走原已配对 CSV 的 `domain.compute(rows, options)` 口径。

迟到补测通过新的完整两表指纹生成新的 `data_revisions` 版本；旧版本与其 `analysis_runs.result` 不被更新。
