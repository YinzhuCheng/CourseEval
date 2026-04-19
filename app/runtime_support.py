from typing import Final


SUPPORTED_PYTHON_VERSION: Final[str] = "3.12"
SUPPORTED_C_STANDARD: Final[str] = "C11"
SUPPORTED_CPP_STANDARD: Final[str] = "C++17"
RUNNER_REQUIREMENTS_FILE: Final[str] = "runner/requirements.txt"

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


def allowed_c_libraries_text(locale: str = "en") -> str:
    if locale == "zh":
        return "允许使用 C 标准库（C11），例如 stdio.h、stdlib.h、string.h、math.h。系统不预装第三方 C 库。"
    return "Allowed libraries: C standard library (C11), such as stdio.h, stdlib.h, string.h, and math.h. No third-party C libraries are preinstalled."


def allowed_cpp_libraries_text(locale: str = "en") -> str:
    if locale == "zh":
        return "允许使用 C++ 标准库（C++17），例如 iostream、vector、string、algorithm、map、set、queue、stack、cmath。系统不预装第三方 C++ 库。"
    return "Allowed libraries: C++ standard library (C++17), such as iostream, vector, string, algorithm, map, set, queue, stack, and cmath. No third-party C++ libraries are preinstalled."


def default_allowed_code_libraries_text(locale: str = "en") -> str:
    return "\n".join(
        [
            default_allowed_python_libraries_text(locale),
            allowed_c_libraries_text(locale),
            allowed_cpp_libraries_text(locale),
        ]
    )


def default_runtime_package_summary() -> str:
    return "\n".join(
        [
            f"Python {SUPPORTED_PYTHON_VERSION}",
            f"C {SUPPORTED_C_STANDARD} via gcc",
            f"C++ {SUPPORTED_CPP_STANDARD} via g++",
            *[
                f"- {package['name']}=={package['version']}"
                for package in SUPPORTED_PYTHON_PACKAGES
            ],
        ]
    )
