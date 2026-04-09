from collections.abc import Callable

from starlette.requests import Request

from app.config import get_settings
from app.constants import Locale


settings = get_settings()
SUPPORTED_LOCALES = {Locale.EN.value, Locale.ZH.value}


TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "site.title": "Notebook Runner",
        "nav.my_courses": "My Courses",
        "nav.runner_dashboard": "Runner Dashboard",
        "nav.teacher": "Teacher",
        "nav.admin": "Admin",
        "nav.login": "Login",
        "nav.register": "Register",
        "nav.logout": "Logout",
        "nav.signed_in_as": "Signed in as {username}",
        "locale.switch_to_en": "English",
        "locale.switch_to_zh": "中文",
        "register.title": "Create an account",
        "register.subtitle": "Choose your role, complete the required information, and start using the platform.",
        "register.username": "Username",
        "register.email": "Email",
        "register.role": "Register as",
        "register.role.student": "Student",
        "register.role.student.help": "Students can join courses, submit answers, and view their grades and feedback.",
        "register.role.teacher": "Teacher",
        "register.role.teacher.help": "Teachers can create courses, configure assignments, and review submissions.",
        "register.teacher_code": "Teacher registration code",
        "register.teacher_code.help": "Required only for teacher accounts.",
        "register.password": "Password",
        "register.confirm_password": "Confirm password",
        "register.password.requirements": "Password requirements",
        "register.password.rule.length": "At least 8 characters",
        "register.password.rule.match": "Password and confirmation must match",
        "register.password.rule.security": "Use a mix of letters, numbers, and symbols for better security",
        "register.password.note": "For security reasons, passwords are not preserved after a failed submission. Other fields stay filled in.",
        "register.submit": "Register",
        "register.have_account": "Already have an account?",
        "register.sign_in": "Sign in",
        "register.validation_failed": "Please review the highlighted registration fields and try again.",
        "login.title": "Sign in",
        "login.subtitle": "Use your username or email to access your courses, submissions, and notebook jobs.",
        "login.login": "Username or email",
        "login.password": "Password",
        "login.submit": "Login",
        "login.need_account": "Need an account?",
        "login.register_here": "Register here",
        "student.courses.title": "My courses",
        "student.courses.subtitle": "Courses where you can view assignments and submit answers.",
        "student.courses.empty_title": "No courses yet",
        "student.courses.empty_body": "You are not enrolled in any course. Ask a teacher to add you.",
        "student.courses.open": "Open course",
        "student.courses.no_description": "No course description yet.",
        "teacher.courses.create_title": "Create course",
        "teacher.courses.code": "Course code",
        "teacher.courses.title_label": "Title",
        "teacher.courses.description": "Description",
        "teacher.courses.submit": "Create course",
        "teacher.courses.list_title": "Your teaching courses",
        "teacher.courses.empty_title": "No courses yet",
        "teacher.courses.empty_body": "Create your first course from the form on the left.",
        "teacher.courses.no_description": "No description.",
        "dashboard.title": "Your notebook jobs",
        "dashboard.subtitle": "Track uploads, execution progress, and generated artifacts.",
        "dashboard.upload": "Upload notebook",
        "dashboard.task_id": "Task ID",
        "dashboard.notebook": "Notebook",
        "dashboard.status": "Status",
        "dashboard.created": "Created",
        "dashboard.started": "Started",
        "dashboard.finished": "Finished",
        "dashboard.details": "Details",
        "dashboard.empty_title": "No notebook jobs yet",
        "dashboard.empty_body": "Upload your first .ipynb file to validate the full runner flow.",
        "dashboard.empty_action": "Create your first job",
        "flash.auth_required": "Please sign in to continue.",
        "flash.invalid_email": "Please enter a valid email address.",
        "flash.all_fields_required": "All required fields must be completed.",
        "flash.password_mismatch": "Password confirmation does not match.",
        "flash.password_length": "Password must be at least 8 characters long.",
        "flash.username_email_exists": "Username or email is already registered.",
        "flash.teacher_code_required": "Teacher registration code is required for teacher accounts.",
        "flash.teacher_code_invalid": "Teacher registration code is incorrect.",
        "flash.registration_success": "Registration successful. Welcome!",
        "flash.invalid_login": "Invalid username/email or password.",
        "flash.login_success": "Signed in successfully.",
        "flash.admin_required": "Administrator access is required.",
        "flash.teacher_account_required": "Your account is registered as a student. Teacher features require a teacher account.",
    },
    "zh": {
        "site.title": "Notebook 作业平台",
        "nav.my_courses": "我的课程",
        "nav.runner_dashboard": "运行器面板",
        "nav.teacher": "教师端",
        "nav.admin": "管理端",
        "nav.login": "登录",
        "nav.register": "注册",
        "nav.logout": "退出登录",
        "nav.signed_in_as": "当前登录：{username}",
        "locale.switch_to_en": "English",
        "locale.switch_to_zh": "中文",
        "register.title": "创建账号",
        "register.subtitle": "选择你的身份，填写必要信息，然后开始使用平台。",
        "register.username": "用户名",
        "register.email": "邮箱",
        "register.role": "注册身份",
        "register.role.student": "学生",
        "register.role.student.help": "学生可以加入课程、提交答案并查看成绩与反馈。",
        "register.role.teacher": "教师",
        "register.role.teacher.help": "教师可以创建课程、配置作业并查看学生提交。",
        "register.teacher_code": "教师注册码",
        "register.teacher_code.help": "仅教师账号注册时需要填写。",
        "register.password": "密码",
        "register.confirm_password": "确认密码",
        "register.password.requirements": "密码要求",
        "register.password.rule.length": "至少 8 个字符",
        "register.password.rule.match": "密码与确认密码必须一致",
        "register.password.rule.security": "建议包含字母、数字和符号以提高安全性",
        "register.password.note": "出于安全考虑，注册失败后不会保留密码字段，但其他字段会自动保留。",
        "register.submit": "注册",
        "register.have_account": "已有账号？",
        "register.sign_in": "去登录",
        "register.validation_failed": "请检查标红或提示的注册字段后重试。",
        "login.title": "登录",
        "login.subtitle": "使用用户名或邮箱登录，进入课程、提交记录和 Notebook 运行结果。",
        "login.login": "用户名或邮箱",
        "login.password": "密码",
        "login.submit": "登录",
        "login.need_account": "还没有账号？",
        "login.register_here": "立即注册",
        "student.courses.title": "我的课程",
        "student.courses.subtitle": "你可以在这些课程中查看作业并提交答案。",
        "student.courses.empty_title": "暂无课程",
        "student.courses.empty_body": "你当前还没有加入任何课程，请联系教师将你加入课程。",
        "student.courses.open": "进入课程",
        "student.courses.no_description": "课程暂时还没有描述。",
        "teacher.courses.create_title": "创建课程",
        "teacher.courses.code": "课程编号",
        "teacher.courses.title_label": "课程标题",
        "teacher.courses.description": "课程描述",
        "teacher.courses.submit": "创建课程",
        "teacher.courses.list_title": "我负责的课程",
        "teacher.courses.empty_title": "暂无课程",
        "teacher.courses.empty_body": "从左侧表单创建第一门课程。",
        "teacher.courses.no_description": "暂无描述。",
        "dashboard.title": "Notebook 运行任务",
        "dashboard.subtitle": "查看上传记录、执行进度和生成产物。",
        "dashboard.upload": "上传 Notebook",
        "dashboard.task_id": "任务 ID",
        "dashboard.notebook": "Notebook 文件",
        "dashboard.status": "状态",
        "dashboard.created": "创建时间",
        "dashboard.started": "开始时间",
        "dashboard.finished": "结束时间",
        "dashboard.details": "详情",
        "dashboard.empty_title": "还没有 Notebook 任务",
        "dashboard.empty_body": "上传第一个 .ipynb 文件，验证完整执行链路。",
        "dashboard.empty_action": "创建第一个任务",
        "flash.auth_required": "请先登录后继续。",
        "flash.invalid_email": "请输入有效的邮箱地址。",
        "flash.all_fields_required": "请完整填写所有必填项。",
        "flash.password_mismatch": "两次输入的密码不一致。",
        "flash.password_length": "密码长度至少需要 8 个字符。",
        "flash.username_email_exists": "用户名或邮箱已被注册。",
        "flash.teacher_code_required": "注册教师账号时必须填写教师注册码。",
        "flash.teacher_code_invalid": "教师注册码不正确。",
        "flash.registration_success": "注册成功，欢迎使用！",
        "flash.invalid_login": "用户名/邮箱或密码错误。",
        "flash.login_success": "登录成功。",
        "flash.admin_required": "需要管理员权限才能访问该页面。",
        "flash.teacher_account_required": "当前账号注册身份为学生，教师功能仅对教师账号开放。",
    },
}


def get_locale(request: Request | None) -> str:
    if request is None:
        return settings.default_locale if settings.default_locale in SUPPORTED_LOCALES else Locale.EN.value
    locale = request.session.get("locale") or settings.default_locale
    return locale if locale in SUPPORTED_LOCALES else Locale.EN.value


def set_locale(request: Request, locale: str) -> str:
    normalized = locale if locale in SUPPORTED_LOCALES else Locale.EN.value
    request.session["locale"] = normalized
    return normalized


def translate(locale: str, key: str, **kwargs) -> str:
    catalog = TRANSLATIONS.get(locale, TRANSLATIONS[Locale.EN.value])
    template = catalog.get(key) or TRANSLATIONS[Locale.EN.value].get(key) or key
    return template.format(**kwargs)


def t(request: Request | None, key: str, **kwargs) -> str:
    return translate(get_locale(request), key, **kwargs)


def template_translator(request: Request) -> Callable[[str], str]:
    def _translate(key: str, **kwargs) -> str:
        return t(request, key, **kwargs)

    return _translate
