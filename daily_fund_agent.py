#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
基金每日自动更新脚本（配合系统定时任务跑）
==========================================

对 FUND_MAP 里的每只基金，自动完成：
  1. 增量更新本地缓存——只补抓"上次之后新增的交易日"，不每天重爬 3 年，
     既快又对数据源友好（少打扰人家服务器）。
  2. 计算均线，生成每只基金的交互式 HTML。
  3. 生成一个总览 index.html：列出全部基金 + 最新净值 + 当日涨跌，
     点名字就能跳到对应走势图。

配合 Windows 任务计划 / Linux cron，即可每天定时自动跑，不用再手动输代码。

----------------------------------------------------------------------
Daily automatic fund-update script (run it via a system scheduler).

For every fund in FUND_MAP it automatically:
  1. Incrementally updates the local cache — only fetching trading days added
     since last time, instead of re-scraping 3 years every day (fast, and
     friendly to the data source).
  2. Computes moving averages and generates an interactive HTML per fund.
  3. Builds an overview index.html listing all funds + latest NAV + daily
     change, where clicking a name jumps to its chart.

Pair it with Windows Task Scheduler / Linux cron to run automatically every
day, so you never have to type fund codes by hand again.
"""

import os
import re
import traceback
from datetime import datetime, timedelta

import pandas as pd

from fund_analyzer import FundAnalyzer, FUND_MAP


# ==================== 配置（按需改这里） ====================
# 所有路径都锚定到本脚本所在目录，跑的时候不管在哪个工作目录都不会乱。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WATCHLIST = list(FUND_MAP.keys())   # 监控哪些基金，默认 = 映射表里全部
HISTORY_YEARS = 3                   # 首次（无缓存时）回溯几年
DATA_DIR = os.path.join(BASE_DIR, "data")   # 净值缓存目录：每只基金一个 csv（累积，不分日期）
SOURCE = "auto"                     # auto / akshare / eastmoney / mock
MA_PERIODS = [5, 10, 20, 60, 100]


# ==================== 小工具 ====================
def _log(msg: str):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def _cache_path(code: str) -> str:
    return os.path.join(DATA_DIR, f"{code}.csv")


def load_cache(code: str) -> pd.DataFrame:
    """读本地缓存；没有就返回空表。"""
    p = _cache_path(code)
    if os.path.exists(p):
        return pd.read_csv(p, parse_dates=["净值日期"])
    return pd.DataFrame(columns=["净值日期", "单位净值"])


def save_cache(code: str, df: pd.DataFrame):
    df.to_csv(_cache_path(code), index=False)


def fetch_range(code: str, start: str, end: str) -> pd.DataFrame:
    """
    安静地按 SOURCE 抓一段区间的数据（不打印面向用户的警告）。
    复用 FundAnalyzer 里的数据源方法，auto 时按 akshare->eastmoney->mock 降级。
    """
    fa = FundAnalyzer()
    loaders = {
        "akshare": fa._fetch_from_akshare,
        "eastmoney": fa._fetch_from_eastmoney,
        "mock": fa._generate_mock,
    }
    order = ["akshare", "eastmoney", "mock"] if SOURCE == "auto" else [SOURCE]
    for name in order:
        try:
            df = loaders[name](code, start, end)
            if df is not None and not df.empty:
                return fa._clean(df)
        except Exception as e:
            _log(f"    {code} 经 {name} 失败：{e}")
    return pd.DataFrame(columns=["净值日期", "单位净值"])


# ==================== 核心：增量更新一只基金 ====================
def update_one(code: str) -> pd.DataFrame:
    """读缓存 -> 只补抓新增日期 -> 合并去重 -> 回写缓存 -> 返回完整历史。"""
    cache = load_cache(code)
    today = datetime.today().date()

    if cache.empty:
        # 首次：回溯 HISTORY_YEARS 年
        start = (datetime.today() - timedelta(days=365 * HISTORY_YEARS)).strftime("%Y-%m-%d")
    else:
        last = cache["净值日期"].max().date()
        if last >= today:
            return cache  # 已是最新，连网都不用连
        start = (last + timedelta(days=1)).strftime("%Y-%m-%d")

    end = today.strftime("%Y-%m-%d")
    new = fetch_range(code, start, end)

    if not new.empty:
        cache = (pd.concat([cache, new], ignore_index=True)
                   .drop_duplicates(subset=["净值日期"], keep="last")
                   .sort_values("净值日期")
                   .reset_index(drop=True))
        save_cache(code, cache)
    return cache


# ==================== 生成总览页 ====================
def build_index(summary: list, out_dir: str):
    rows = []
    for s in sorted(summary, key=lambda x: x["code"]):
        # 国内习惯：红涨绿跌
        color = "#c0392b" if s["chg"] >= 0 else "#27ae60"
        sign = "+" if s["chg"] >= 0 else ""
        rows.append(
            f'<tr><td>{s["code"]}</td>'
            f'<td><a href="{s["file"]}">{s["name"]}</a></td>'
            f'<td>{s["date"]}</td>'
            f'<td>{s["nav"]:.4f}</td>'
            f'<td style="color:{color}">{sign}{s["chg"] * 100:.2f}%</td></tr>'
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>基金净值总览</title>
<style>
 body{{font-family:"Microsoft YaHei","PingFang SC","WenQuanYi Micro Hei",sans-serif;
      max-width:780px;margin:40px auto;padding:0 16px;color:#222}}
 h1{{font-size:22px;margin-bottom:4px}}
 .upd{{color:#888;font-size:13px;margin-bottom:18px}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{padding:10px 12px;border-bottom:1px solid #eee;text-align:left;font-size:14px}}
 th{{background:#fafafa;font-weight:600}}
 tr:hover td{{background:#fafcff}}
 a{{color:#2c6fbb;text-decoration:none}} a:hover{{text-decoration:underline}}
</style></head>
<body>
 <h1>基金净值总览</h1>
 <div class="upd">更新时间：{datetime.now():%Y-%m-%d %H:%M}　·　共 {len(summary)} 只基金　·　点名称看走势图</div>
 <table>
  <tr><th>代码</th><th>名称</th><th>最新日期</th><th>单位净值</th><th>当日涨跌</th></tr>
  {''.join(rows)}
 </table>
</body></html>"""

    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


# ==================== 主流程：跑一遍所有基金 ====================
def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    _log(f"开始更新，共 {len(WATCHLIST)} 只基金 ...")
    summary = []
    date_dir = None   # 当天的输出目录，由 export_html 实际写入位置确定

    for code in WATCHLIST:
        name = FUND_MAP.get(code, f"基金{code}")
        try:
            df = update_one(code)
            if df.empty:
                _log(f"  跳过 {code} {name}：无数据")
                continue

            # 算均线 + 出图。export_html 会自动放进「BASE_DIR/当天日期/」目录，
            # 并把实际写入的完整路径返回，我们据此让 index.html 落在同一个目录。
            fa = FundAnalyzer()
            fa.load_dataframe(code, df)
            fa.calculate_ma(MA_PERIODS)

            safe_name = re.sub(r'[\\/:*?"<>|\s]', "", name)
            out_name = f"fund_{code}_{safe_name}.html"
            saved = fa.export_html(out_name, include_plotlyjs="cdn", base_dir=BASE_DIR)
            date_dir = os.path.dirname(saved)   # 当天目录

            # 汇总：最新净值 + 当日涨跌（index 链接用实际文件名，绝不会对不上）
            tail = df.tail(2)["单位净值"].tolist()
            chg = (tail[-1] / tail[-2] - 1) if len(tail) == 2 else 0.0
            summary.append({
                "code": code, "name": name,
                "date": df["净值日期"].max().date().isoformat(),
                "nav": tail[-1], "chg": chg, "file": os.path.basename(saved),
            })
            _log(f"  完成 {code} {name}：{df['净值日期'].max().date()} 净值 {tail[-1]:.4f}")

        except Exception:
            # 单只基金出错不影响其它基金，记日志后继续
            _log(f"  出错 {code} {name}:\n{traceback.format_exc()}")

    if date_dir and summary:
        build_index(summary, date_dir)
        _log(f"全部完成。打开 {os.path.join(date_dir, 'index.html')} 查看总览。")
    else:
        _log("没有任何基金成功生成，未输出总览页。")


if __name__ == "__main__":
    run()
