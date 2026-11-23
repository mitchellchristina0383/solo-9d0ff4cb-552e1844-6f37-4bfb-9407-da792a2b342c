"""容器入口：创建应用并启动后台调度线程。"""
from inspector.app import create_app

app = create_app()
app.scheduler.start()

if __name__ == "__main__":
    # 单进程 + 多线程即可满足本巡检台；threaded 默认开启。
    app.run(host="0.0.0.0", port=8000, threaded=True)
