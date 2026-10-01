from django.urls import path
from . import views
from .payments import initialize_payment, paystack_webhook

urlpatterns = [
    # Authentication & Public Pages
    path("", views.landing_page, name="landing"),
    path("accounts/signup/", views.signup_view, name="signup"),
    path("accounts/login/", views.login_view, name="login"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("dashboard/", views.dashboard_view, name="dashboard"),

    # Exam Taking (Updated name to 'exam_page' to match template & views)
    path("exam/", views.take_exam, name="take_exam"),
    path("exam/<int:subject_id>/", views.exam_page_view, name="exam_page"),
    
    # Exam API Endpoints (Matched to the URL patterns fetched by your exam.html JavaScript)
    path("exam/attempt/<int:subject_id>/start/", views.start_exam_attempt, name="start_exam_attempt"),
    path("exam/attempt/<int:attempt_id>/question/<int:question_id>/", views.get_question, name="get_question"),
    path("exam/attempt/<int:attempt_id>/submit/", views.submit_answer, name="submit_answer"),
    path("exam/attempt/<int:attempt_id>/results/", views.get_attempt_results, name="get_attempt_results"),

    # Paystack Payment Integration
    path("api/payments/initialize/", initialize_payment, name="initialize-payment"),
    path("api/payments/webhook/", paystack_webhook, name="paystack_webhook"),

    # University Tier & Study Materials
    path("api/university/upload/", views.upload_course_material, name="upload-material"),
    path("api/university/study-session/<int:material_id>/", views.get_study_session, name="study-session"),
    path("api/university/generate-questions/<int:material_id>/", views.get_study_session, name="generate-questions"),
    
    # AI Study Assistant Chat Routes
    path("api/university/ask/", views.ask_study_assistant, name="general-study-assistant"),
    path("api/university/study-session/<int:material_id>/ask/", views.ask_study_assistant, name="ask-study-assistant"),
    
    # Legacy fallback for AI study chat
    path("study/general/ask/", views.ask_study_assistant, name="general-ask-legacy"),

    # External Past Questions API Integration
    path("api/external-questions/", views.fetch_external_past_questions, name="fetch_external_past_questions"),
]