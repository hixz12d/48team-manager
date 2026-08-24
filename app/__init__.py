"""
48 Team Manager

版本号在此处单点维护：config.app_version / FastAPI(version=...) /
模板页脚均会读取下面的 __version__。发版流程为：改动此处 → 提 PR 合并 →
推 git tag v{__version__} → GitHub Action 自动建 Release。
"""
__version__ = "1.0.0"
