"""安装时初始化个人配置；不创建代码目录中的凭据文件。"""
from helperme.config import load_app_config, write_json
from helperme.llm.config import initial_connections, load_connections
from helperme.paths import HelperMeHome


def initialize():
    home = HelperMeHome.default()
    home.initialize()
    load_app_config()
    if not home.connections_path.exists():
        write_json(home.connections_path, initial_connections())
    load_connections(home.connections_path)
    return home


if __name__ == "__main__":
    home = initialize()
    print(f"个人模型连接配置：{home.connections_path}")
