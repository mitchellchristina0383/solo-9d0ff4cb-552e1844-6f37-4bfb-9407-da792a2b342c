"""Flask 巡检台应用工厂与路由。"""
from __future__ import annotations

import os
from datetime import datetime

from flask import Flask, flash, redirect, render_template, request, url_for

from . import timeutil
from .scheduler import Scheduler, default_disk_reader
from .storage import Storage


def create_app(db_path: str | None = None,
               scheduler: Scheduler | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-inspector-secret")

    if scheduler is None:
        db_path = db_path or os.environ.get("DB_PATH", "/data/inspector.db")
        storage = Storage(db_path)
        volume = os.environ.get("VOLUME_PATH", "/volume")
        read_delay = float(os.environ.get("READ_DELAY_SECONDS", "0"))
        poll = float(os.environ.get("POLL_INTERVAL_SECONDS", "1"))
        scheduler = Scheduler(
            storage=storage,
            volume_path=volume,
            disk_reader=default_disk_reader,
            read_delay=read_delay,
            poll_interval=poll,
        )
    app.scheduler = scheduler  # type: ignore[attr-defined]

    def _trigger():
        scheduler.wake()

    @app.post("/check-time")
    def update_check_time():
        hhmm = request.form.get("check_time", "").strip()
        try:
            scheduler.set_check_time(hhmm)
        except timeutil.InvalidTimeError as exc:
            flash(f"时刻无效：{exc}", "error")
        else:
            flash(f"每日巡检时刻已更新为 {hhmm}", "ok")
            _trigger()
        return redirect(url_for("index"))

    @app.post("/clock/set")
    def clock_set():
        value = request.form.get("utc_time", "").strip()
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            flash("演练时钟格式无效，请用 ISO 格式，如 2026-03-08T13:00", "error")
            return redirect(url_for("index"))
        scheduler.set_clock(dt)
        flash("演练时钟已设定", "ok")
        _trigger()
        return redirect(url_for("index"))

    @app.post("/clock/advance")
    def clock_advance():
        raw = request.form.get("seconds", "").strip()
        try:
            seconds = float(raw)
        except ValueError:
            flash("推进秒数无效", "error")
            return redirect(url_for("index"))
        scheduler.advance_clock(seconds)
        flash(f"演练时钟已推进 {seconds:g} 秒", "ok")
        _trigger()
        return redirect(url_for("index"))

    @app.post("/clock/reset")
    def clock_reset():
        scheduler.reset_clock()
        flash("演练时钟已恢复为真实时间", "ok")
        _trigger()
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        st = scheduler.status()
        plan_view = None
        if st["plan"]:
            p = st["plan"]
            planned_ny = timeutil.from_iso(p["planned_utc"]).astimezone(
                timeutil.NY_TZ
            )
            plan_view = {
                **p,
                "planned_local": planned_ny.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
                "dst_label": {
                    "normal": "正常时刻",
                    "gap_deferred": "春季跳时：已顺延到跳时后第一个有效分钟",
                    "ambiguous_first": "秋季重复时刻：取第一次出现",
                }.get(p["dst_note"], p["dst_note"]),
            }
        result_view = None
        if st["result"]:
            r = dict(st["result"])
            r["finished_local"] = timeutil.from_iso(
                r["finished_utc"]
            ).astimezone(timeutil.NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z%z")
            r["planned_local"] = timeutil.from_iso(
                r["planned_utc"]
            ).astimezone(timeutil.NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z%z")
            result_view = r
        return render_template(
            "index.html",
            st=st,
            plan_view=plan_view,
            result_view=result_view,
        )

    return app
