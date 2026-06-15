#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
基金均线计算与交互式可视化
==========================

设计原则（为后续做网站预留接口）：
    1. 数据层 / 计算层 / 可视化层 完全分离。
    2. 所有"取数 + 计算"的方法都返回 pandas.DataFrame，
       可直接被 Flask / FastAPI 当作 API 的数据源。
    3. plot_interactive() 只负责把 DataFrame 变成 Plotly Figure，
       不关心数据从哪来，Figure 可直接在 Flask 中渲染。

数据来源优先级：akshare -> 东方财富(eastmoney) -> 模拟数据。
任意一个失败会自动降级，保证演示永远能跑通。

----------------------------------------------------------------------
Fund moving-average calculation & interactive visualization.

Design principles (keeping the door open for a future web app):
    1. Data layer / computation layer / visualization layer are fully decoupled.
    2. Every "fetch + compute" method returns a pandas.DataFrame, so it can
       serve directly as the data source for a Flask / FastAPI endpoint.
    3. plot_interactive() only turns a DataFrame into a Plotly Figure; it does
       not care where the data came from, and the Figure can be rendered in Flask.

Data-source priority: akshare -> Eastmoney -> mock data. If one fails it
automatically falls back to the next, so the demo always runs.
"""

from __future__ import annotations

import os
import re
import time
import platform
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import numpy as np
import pandas as pd
import plotly.graph_objects as go


# ============================================================
# 0. 配置：基金代码 -> 名称映射（要扩展直接往这里加）
# ============================================================
FUND_MAP: Dict[str, str] = {
    "021492": "中航远见领航C_端侧",
    "021490": "中航趋势领航C_机器人",
    "016076": "华夏制造升级混合C_机器人",
    "014409": "创金合信精选产业趋势混合C_机器人",
    "017526": "华夏北证50指数C",
    "026681": "永赢产业机遇智选混合发起C_电网设备",
    "007690": "国投瑞银新能源混合C新能源",
    "025957": "新华科技优选混合发起式C_消费电子",
    "003305": "前海开源沪港深核心资源灵活配置混合C_有色金属",
    "016389": "汇安均衡成长混合C_算力租赁",
    "002052": "诺安稳健回报灵活配置混合C_万得人工智能指数",
}

# 浏览器端中文字体栈：依次尝试各平台常见中文字体，命中即用。
# 这是 Plotly 解决跨平台中文乱码的关键——渲染交给浏览器，
# 不再依赖本机 matplotlib 是否装了某个 .ttf 文件。
CHINESE_FONT_STACK = (
    '"Microsoft YaHei", "微软雅黑", '          # Windows
    '"PingFang SC", "Hiragino Sans GB", '      # macOS
    '"Heiti SC", "STHeiti", '                  # macOS 备选
    '"WenQuanYi Micro Hei", "Noto Sans CJK SC", '  # Linux
    'sans-serif'
)


# ============================================================
# 1. 核心类
# ============================================================
class FundAnalyzer:
    """
    基金分析器。

    典型用法：
        fa = FundAnalyzer()
        fa.fetch_data("021492", "2023-06-09", "2026-06-09", source="mock")
        fa.calculate_ma([5, 10, 20, 60])
        fig = fa.plot_interactive()
        fa.export_html("fund.html")
    """

    def __init__(self, fund_map: Optional[Dict[str, str]] = None):
        # 允许外部传入自定义映射，方便单元测试 / 扩展
        self.fund_map: Dict[str, str] = fund_map if fund_map is not None else dict(FUND_MAP)

        # 运行期状态
        self.fund_code: Optional[str] = None
        self.fund_name: Optional[str] = None
        self.df: Optional[pd.DataFrame] = None          # 原始净值
        self.ma_periods: List[int] = []                 # 已计算的均线周期

    # ---------- 工具：根据代码取名字 ----------
    def get_fund_name(self, fund_code: str) -> str:
        return self.fund_map.get(fund_code, f"基金{fund_code}")

    # ========================================================
    # 1.1 数据获取（对外统一入口）
    # ========================================================
    def fetch_data(
        self,
        fund_code: str,
        start_date: str,
        end_date: str,
        source: str = "auto",
    ) -> pd.DataFrame:
        """
        获取基金历史净值。

        参数:
            fund_code  : 6 位基金代码
            start_date : "YYYY-MM-DD"
            end_date   : "YYYY-MM-DD"
            source     : "auto" | "akshare" | "eastmoney" | "mock"
                         auto 会按 akshare -> eastmoney -> mock 依次降级。

        返回:
            DataFrame，列固定为 ['净值日期', '单位净值']（日期升序、已去重）。
            —— 这个稳定的返回结构就是给后续 API 用的契约。
        """
        self.fund_code = fund_code
        self.fund_name = self.get_fund_name(fund_code)

        loaders = {
            "akshare": self._fetch_from_akshare,
            "eastmoney": self._fetch_from_eastmoney,
            "mock": self._generate_mock,
        }

        if source == "auto":
            order = ["akshare", "eastmoney", "mock"]
        else:
            order = [source]

        last_err: Optional[Exception] = None
        for name in order:
            try:
                df = loaders[name](fund_code, start_date, end_date)
                if df is not None and not df.empty:
                    self.df = self._clean(df)
                    n = len(self.df)
                    d0 = self.df["净值日期"].min().date()
                    d1 = self.df["净值日期"].max().date()
                    print(f"[数据] 已通过 {name} 获取 {n} 条净值记录"
                          f"（{d0} ~ {d1}）。")
                    # 数据太少时，长均线（尤其 MA100）没有意义，提前提醒
                    if n < 100:
                        print(f"[警告] 仅 {n} 条数据，不足 100 条，"
                              "100 日均线会几乎等于净值本身，参考价值有限。")
                    return self.df
            except Exception as e:  # 某个源失败就降级到下一个
                last_err = e
                print(f"[数据] {name} 获取失败：{e}，尝试下一个数据源 ...")

        raise RuntimeError(f"所有数据源均失败，最后错误：{last_err}")

    # ---------- 数据源 A：akshare ----------
    @staticmethod
    def _fetch_from_akshare(fund_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        akshare 接口（真实数据）。需要 `pip install akshare`。
        fund_open_fund_info_em 返回开放式基金的历史净值。
        """
        import akshare as ak  # 延迟导入：没装也不影响其它功能

        # akshare 不同版本参数名不同：新版用 fund=，旧版用 symbol=，两个都试
        try:
            raw = ak.fund_open_fund_info_em(fund=fund_code, indicator="单位净值走势")
        except TypeError:
            raw = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
        # akshare 返回列通常是 ['净值日期', '单位净值', '日增长率']
        df = raw[["净值日期", "单位净值"]].copy()
        df["净值日期"] = pd.to_datetime(df["净值日期"])
        mask = (df["净值日期"] >= pd.to_datetime(start_date)) & (df["净值日期"] <= pd.to_datetime(end_date))
        return df.loc[mask]

    # ---------- 数据源 B：东方财富（你原来的实现，整理进类里）----------
    @staticmethod
    def _fetch_from_eastmoney(
        fund_code: str,
        start_date: str,
        end_date: str,
        per: int = 40,
        sleep_time: float = 0.3,
        timeout: int = 15,
    ) -> pd.DataFrame:
        """从天天基金 F10DataApi 抓取并自动翻页。"""
        import requests  # 延迟导入
        from io import StringIO  # 用它包住 html 文本，消除 read_html 的 FutureWarning

        base_url = "http://fund.eastmoney.com/f10/F10DataApi.aspx"
        common = {"type": "lsjz", "code": fund_code,
                  "sdate": start_date, "edate": end_date, "per": str(per)}

        session = requests.Session()
        session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/91.0 Safari/537.36"),
            "Referer": f"http://fund.eastmoney.com/f10/jjjz_{fund_code}.html",
        })

        # 第 1 页：拿总页数 pages 和总条数 records
        resp = session.get(base_url, params={**common, "page": 1}, timeout=timeout)
        resp.raise_for_status()
        text = resp.text

        m_pages = re.search(r"pages:\s*(\d+)", text)
        m_records = re.search(r"records:\s*(\d+)", text)  # 接口自报的总条数
        if not m_pages:
            raise RuntimeError("无法解析 pages，接口可能已变更或被反爬拦截。")
        total_pages = int(m_pages.group(1))
        records = int(m_records.group(1)) if m_records else None

        # 接口直接说这个区间 0 条 → 多半是基金太新或代码不对，直接报清楚
        if records == 0:
            raise RuntimeError(
                f"东方财富返回 records=0：基金 {fund_code} 在 "
                f"{start_date}~{end_date} 区间内没有净值数据"
                "（常见原因：基金太新、代码输错、或该代码不是开放式基金）。"
            )

        all_dfs = [pd.read_html(StringIO(text))[0]]
        time.sleep(sleep_time)
        for page in range(2, total_pages + 1):
            resp = session.get(base_url, params={**common, "page": page}, timeout=timeout)
            resp.raise_for_status()
            dfs = pd.read_html(StringIO(resp.text))
            if dfs:
                all_dfs.append(dfs[0])
            time.sleep(sleep_time)

        df = pd.concat(all_dfs, ignore_index=True)
        cols = list(df.columns)
        df = df.rename(columns={cols[0]: "净值日期", cols[1]: "单位净值"})
        df = df[["净值日期", "单位净值"]]

        # 抓到的行数 与 接口自报 records 对不上 → 给出警告（不报错，仍返回已抓到的）
        if records is not None and len(df) < records:
            print(f"[警告] 接口自报 {records} 条，实际只抓到 {len(df)} 条，"
                  "可能被分页/反爬截断，建议改用 akshare。")
        return df

    # ---------- 数据源 C：模拟数据（兜底，演示用）----------
    @staticmethod
    def _generate_mock(fund_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        用几何布朗运动生成一段"像真的"净值序列，范围大致 1.0~1.6，
        只在工作日生成，方便离线演示。
        """
        rng = np.random.default_rng(seed=int(fund_code) % 9973)  # 同一基金每次一样，便于对比
        dates = pd.bdate_range(start=start_date, end=end_date)   # 仅工作日
        n = len(dates)

        mu, sigma = 0.0002, 0.012          # 日漂移 / 日波动
        shocks = rng.normal(mu, sigma, n)
        nav = 1.0 * np.exp(np.cumsum(shocks))
        nav = np.clip(nav, 0.85, 1.75)     # 防止跑太远

        return pd.DataFrame({"净值日期": dates, "单位净值": np.round(nav, 4)})

    # ---------- 清洗（所有数据源出口统一过这里）----------
    @staticmethod
    def _clean(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["净值日期"] = pd.to_datetime(df["净值日期"], errors="coerce")
        df["单位净值"] = pd.to_numeric(df["单位净值"], errors="coerce")
        df = (df.dropna(subset=["净值日期", "单位净值"])
                .drop_duplicates(subset=["净值日期"], keep="first")
                .sort_values("净值日期")
                .reset_index(drop=True))
        return df

    # ========================================================
    # 1.2 均线计算
    # ========================================================
    def calculate_ma(self, periods: List[int] = [5, 10, 20, 60, 100]) -> pd.DataFrame:
        """
        计算移动平均线，结果直接写回 self.df 并返回。
        每条均线对应一列：MA5 / MA10 / MA20 / MA60 ...
        """
        if self.df is None:
            raise RuntimeError("请先调用 fetch_data 获取数据。")

        self.ma_periods = list(periods)
        for p in periods:
            # min_periods=1：前期数据不足时也给出当前已有数据的均值，
            # 避免开头一长段空白（按需也可设为 =p 让它留空）
            self.df[f"MA{p}"] = self.df["单位净值"].rolling(window=p, min_periods=1).mean()
        return self.df

    # ========================================================
    # 1.3 交互式可视化（只吃 DataFrame，吐 Figure）
    # ========================================================
    def plot_interactive(self, kind: str = "candle") -> go.Figure:
        """
        生成 Plotly 交互图（净值线 / K线 二合一，按钮一键切换）。

        参数:
            kind : "candle" 默认先显示 K 线；"line" 先显示净值线。
                   两种都画进同一张图，右上角按钮随时切换，均线始终叠加。

        关于 K 线：基金每天只有一个单位净值，没有真正的开高低收。
        这里沿用常见做法——用「昨日净值=开盘、今日净值=收盘」合成蜡烛，
        所以每根蜡烛只表示当天涨跌方向（红涨绿跌，国内习惯），没有真实影线。

        返回 go.Figure（可直接 export，也可塞进 Flask）。
        """
        if self.df is None:
            raise RuntimeError("请先调用 fetch_data。")
        if not self.ma_periods:
            self.calculate_ma()

        df = self.df
        ma_cols = [f"MA{p}" for p in self.ma_periods]
        customdata = df[ma_cols].to_numpy()

        # 合成 OHLC：开盘=昨日净值，收盘=今日净值
        nav = df["单位净值"].to_numpy()
        open_ = np.empty_like(nav)
        open_[0] = nav[0]
        open_[1:] = nav[:-1]
        high = np.maximum(open_, nav)
        low = np.minimum(open_, nav)

        # 净值线的 hover（带名称/编号/各均线）
        ma_hover_lines = "".join(
            f"{col}：%{{customdata[{i}]:.4f}}<br>" for i, col in enumerate(ma_cols)
        )
        nav_hovertemplate = (
            f"<b>{self.fund_name}</b>（{self.fund_code}）<br>"
            "日期：%{x|%Y-%m-%d}<br>"
            "<b>单位净值：%{y:.4f}</b><br>"
            + ma_hover_lines
            + "<extra></extra>"
        )

        show_candle = (kind == "candle")
        fig = go.Figure()

        # ---- trace 0：K线（合成蜡烛，红涨绿跌）----
        fig.add_trace(go.Candlestick(
            x=df["净值日期"], open=open_, high=high, low=low, close=nav,
            name="K线", visible=show_candle,
            increasing_line_color="#c0392b", increasing_fillcolor="#c0392b",
            decreasing_line_color="#27ae60", decreasing_fillcolor="#27ae60",
        ))

        # ---- trace 1：净值线 ----
        fig.add_trace(go.Scatter(
            x=df["净值日期"], y=df["单位净值"],
            mode="lines", name="单位净值", visible=not show_candle,
            line=dict(color="#1f1f1f", width=1.8),
            customdata=customdata, hovertemplate=nav_hovertemplate,
        ))

        # ---- trace 2..：各条均线（两种视图下都常驻）----
        ma_colors = {5: "#e6550d", 10: "#3182bd", 20: "#31a354",
                     60: "#756bb1", 100: "#d62728"}
        for p in self.ma_periods:
            fig.add_trace(go.Scatter(
                x=df["净值日期"], y=df[f"MA{p}"],
                mode="lines", name=f"{p}日均线",
                line=dict(color=ma_colors.get(p, None), width=1.2),
                hovertemplate=f"{p}日均线：%{{y:.4f}}<extra></extra>",
            ))

        # 切换按钮的可见性数组：[K线, 净值线, MA...]
        n_ma = len(self.ma_periods)
        vis_candle = [True, False] + [True] * n_ma
        vis_line = [False, True] + [True] * n_ma

        today = datetime.today().strftime("%Y-%m-%d")

        fig.update_layout(
            title=dict(
                text=f"{self.fund_code} {self.fund_name} - 走势图（{today}）",
                font=dict(size=20), x=0.5, xanchor="center",
            ),
            font=dict(family=CHINESE_FONT_STACK, size=13),
            hovermode="x unified",
            template="plotly_white",
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="right", x=1),
            # 左上角：K线 / 净值线 一键切换
            updatemenus=[dict(
                type="buttons", direction="right",
                x=0, xanchor="left", y=1.13, yanchor="top",
                showactive=True,
                buttons=[
                    dict(label="K线", method="update",
                         args=[{"visible": vis_candle}]),
                    dict(label="净值线", method="update",
                         args=[{"visible": vis_line}]),
                ],
            )],
            xaxis=dict(
                title="日期",
                rangeselector=dict(buttons=[
                    dict(count=1, label="1月", step="month", stepmode="backward"),
                    dict(count=3, label="3月", step="month", stepmode="backward"),
                    dict(count=6, label="6月", step="month", stepmode="backward"),
                    dict(count=1, label="1年", step="year", stepmode="backward"),
                    dict(step="all", label="全部"),
                ]),
                rangeslider=dict(visible=True),
                type="date",
            ),
            yaxis=dict(title="净值"),
            margin=dict(l=60, r=40, t=90, b=40),
        )
        return fig

    # ========================================================
    # 1.4 导出 HTML（按当天日期建子目录，把 HTML 存进去）
    # ========================================================
    def export_html(
        self,
        filepath: str,
        include_plotlyjs: str | bool = True,
        base_dir: Optional[str] = None,
        kind: str = "candle",
    ) -> str:
        """
        导出独立 HTML 文件，自动放进「日期子目录」里：
            <base_dir>/<YYYY-MM-DD>/<文件名>.html

        参数:
            filepath        : 只取其中的文件名部分（目录部分会被忽略）
            include_plotlyjs: True 内嵌 plotly.js（大、离线可看）；
                              "cdn" 走 CDN（小、需联网）；
                              "directory" 同目录单独放一份 plotly.min.js（小且离线可看）
            base_dir        : 日期目录建在哪个根目录下；
                              默认 = 本脚本(fund_analyzer.py)所在目录。
                              agent 会传入自己的目录，保证多文件落在同一处。

        返回: 实际写入的完整路径（agent 靠它把 index.html 放到同一个日期目录）。
        """
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))

        today = datetime.today().strftime("%Y-%m-%d")
        date_dir = os.path.join(base_dir, today)
        os.makedirs(date_dir, exist_ok=True)

        # 只取文件名，丢掉传入路径里的目录部分
        _, filename = os.path.split(filepath)
        full_path = os.path.join(date_dir, filename)

        fig = self.plot_interactive(kind=kind)
        fig.write_html(full_path, include_plotlyjs=include_plotlyjs,
                       full_html=True, config={"scrollZoom": True})
        print(f"[导出] HTML 已保存：{full_path}")
        return full_path

    # ========================================================
    # 1.5 给 API 用的便捷出口：直接拿带均线的 DataFrame
    # ========================================================
    def to_dataframe(self) -> pd.DataFrame:
        """返回完整结果（净值 + 各均线），列名稳定，方便序列化成 JSON 给前端。"""
        if self.df is None:
            raise RuntimeError("请先 fetch_data。")
        return self.df.copy()

    def load_dataframe(self, fund_code: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        直接载入一份已有的净值 DataFrame（来自本地缓存 / 数据库 / API），
        跳过网络获取。df 至少要有 ['净值日期', '单位净值'] 两列。
        —— 这是给「定时脚本」和「后续 Web 后端」共用的入口：
           数据从哪来不关心，可视化只认这个干净的 DataFrame。
        """
        self.fund_code = fund_code
        self.fund_name = self.get_fund_name(fund_code)
        self.df = self._clean(df)
        return self.df


# ============================================================
# 2. 命令行演示（交互式：输入一个代码，只画这一个）
# ============================================================
def _analyze_one(fa: FundAnalyzer, fund_code: str):
    """抓一个基金的数据、算均线、导出 HTML。"""
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=365 * 3)

    fa.fetch_data(
        fund_code,
        start_dt.strftime("%Y-%m-%d"),
        end_dt.strftime("%Y-%m-%d"),
        source="auto",
    )
    fa.calculate_ma([5, 10, 20, 60, 100])   # 含百日均线

    # 文件名带上中文名：fund_021490_中航趋势领航C.html
    # 先清洗掉文件系统不允许的字符（\ / : * ? " < > | 和空格）
    safe_name = re.sub(r'[\\/:*?"<>|\s]', "", fa.fund_name)
    out = f"fund_{fund_code}_{safe_name}.html"
    fa.export_html(out)
    print(f"[完成] {fund_code} {fa.fund_name} -> {out}\n")


def main():
    fa = FundAnalyzer()

    # 先把已登记的基金列出来，方便直接挑
    print("=" * 40)
    print("可选基金（也可直接输入任意 6 位代码）：")
    for code, name in fa.fund_map.items():
        print(f"  {code}  {name}")
    print("=" * 40)

    while True:
        code = input("请输入基金代码（直接回车退出）：").strip()
        if not code:
            print("已退出。")
            break
        if not code.isdigit() or len(code) != 6:
            print("  代码应为 6 位数字，请重新输入。\n")
            continue
        try:
            _analyze_one(fa, code)
        except Exception as e:
            print(f"  分析失败：{e}\n")
        # 想一次只查一个就把下面这行删掉/改成 break；保留则可连续查多个


if __name__ == "__main__":
    main()
