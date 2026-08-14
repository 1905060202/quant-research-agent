# 量化验证卡（ES-SOP L1）· QRA 知识条目
猎豹量化系统的验证卡 = ES-SOP L1 门禁的量化验证卡，包含 5 项检查：
1. 信号新鲜度：latest_signal.json 数据日期是否 24h 内
2. 生产信号一致性：signal_history_prod.pkl 最新日期 + 池内 top-3 与今日一致
3. top-N alpha：最近 window_days 交易日 top-1~3 回测年化 vs 等权基准
4. 衰减监控：全窗年化 vs 近 20/60/120 日年化 → 平坦/衰减判定
5. 模拟盘状态：净值/持仓/今日 top-1 建议
用途：日报/研报的「量化信号验证卡」模块；写任何投资报告前必跑（quant-es-sop skill）
