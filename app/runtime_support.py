from typing import Final


SUPPORTED_PYTHON_VERSION: Final[str] = "3.12"

SUPPORTED_PYTHON_PACKAGES: Final[tuple[dict[str, str], ...]] = (
    {
        "name": "numpy",
        "version": "2.4.4",
        "summary_en": "Numerical arrays and vectorized computation",
        "summary_zh": "数值数组与向量化计算",
    },
    {
        "name": "pandas",
        "version": "3.0.2",
        "summary_en": "Tabular data processing",
        "summary_zh": "表格数据处理",
    },
    {
        "name": "matplotlib",
        "version": "3.10.8",
        "summary_en": "Basic plotting and visualization",
        "summary_zh": "基础绘图与可视化",
    },
    {
        "name": "scipy",
        "version": "1.17.1",
        "summary_en": "Scientific computing helpers",
        "summary_zh": "科学计算工具",
    },
    {
        "name": "scikit-learn",
        "version": "1.8.0",
        "summary_en": "Classical machine learning utilities",
        "summary_zh": "经典机器学习工具",
    },
)

UNSUPPORTED_PACKAGE_NOTE_EN: Final[str] = (
    "The default runtime image does not preinstall deep-learning frameworks such as "
    "torch, tensorflow, jax, paddle, mxnet, or transformers."
)
UNSUPPORTED_PACKAGE_NOTE_ZH: Final[str] = (
    "默认运行环境未预装深度学习框架，例如 torch、tensorflow、jax、paddle、mxnet、transformers。"
)


def default_allowed_python_libraries_text(locale: str = "en") -> str:
    package_list = ", ".join(
        f"{package['name']} {package['version']}" for package in SUPPORTED_PYTHON_PACKAGES
    )
    if locale == "zh":
        return (
            f"允许导入 Python 标准库，以及以下预装三方包：{package_list}。"
            f"{UNSUPPORTED_PACKAGE_NOTE_ZH}"
        )
    return (
        f"Allowed imports: Python standard library plus the preinstalled packages "
        f"{package_list}. {UNSUPPORTED_PACKAGE_NOTE_EN}"
    )


def default_runtime_package_summary() -> str:
    return "\n".join(
        [
            f"Python {SUPPORTED_PYTHON_VERSION}",
            *[
                f"- {package['name']}=={package['version']}"
                for package in SUPPORTED_PYTHON_PACKAGES
            ],
        ]
    )
