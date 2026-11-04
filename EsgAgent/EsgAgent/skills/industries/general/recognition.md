---
id: industry.general
version: 1.0.0
kind: industry
applies_to: [general]
depends_on: [common.evidence, common.normalization]
priority: 90
token_estimate: 300
metric_ids: []
name: 通用行业水资源披露识别
---

公司及SASB行业以输入主数据为准，不从报告内容重新猜测行业。
本规则用于尚无专门行业Skill的公司，不套用饮料制造业的工艺或边界假设。
逐项记录水量、排水、回用、收入及污染物指标的原值、年份、单位和实际运营范围。
水务企业的客户供水、污水处理业务量及污染物去除量，不自动等同于自身运营取水或排放。
范围不清、仅场址/业务线、数据年度不明时保留歧义，不默认集团全年。
