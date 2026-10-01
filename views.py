"""
exam_app/views.py
==================
All views for the CBT platform: public pages, authentication, dashboard,
exam-taking, university course-material endpoints powered by Google Gemini,
and external past questions API integration.
"""

import json
import logging
import os
import pypdf
import requests

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .decorators import tier_required
from .models import (
    CourseMaterial,
    ExamAttempt,
    Option,
    Question,
    StudentAnswer,
    StudySession,
    Subject,
    Subscription,
)

logger = logging.getLogger("exam_app")
User = get_user_model()


# =========================================================================
# 1. AUTHENTICATION FORMS
# =========================================================================

class CustomUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    phone_number = forms.CharField(required=False, max_length=17)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "phone_number")

    def clean_email(self):
        email = self.cleaned_data.get("email", "").lower().strip()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("A user with that username already exists.")
        return username


# =========================================================================
# 2. AUTHENTICATION VIEWS
# =========================================================================

def signup_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            request.session['is_new_signup'] = True
            messages.success(request, "Welcome! Your account has been created.")
            return redirect("dashboard")
    else:
        form = CustomUserCreationForm()

    return render(request, "signup.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = AuthenticationForm()

    if request.method == "POST":
        identifier = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = None
        if identifier and password:
            try:
                user_obj = User.objects.get(email__iexact=identifier)
            except User.DoesNotExist:
                try:
                    user_obj = User.objects.get(username__iexact=identifier)
                except User.DoesNotExist:
                    user_obj = None

            if user_obj and check_password(password, user_obj.password):
                user = user_obj

        if user is not None:
            login(request, user)
            request.session.pop('is_new_signup', None)

            next_url = request.POST.get("next") or request.GET.get("next")
            if next_url and url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect("dashboard")
        else:
            messages.error(request, "Please enter a correct email address and password.")

    next_url = request.GET.get("next", "")
    return render(request, "login.html", {"form": form, "next": next_url})


@login_required
def logout_view(request):
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("landing")


# =========================================================================
# 3. PUBLIC PAGES + DASHBOARD
# =========================================================================

def landing_page(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    tiers = [
        {"name": "WAEC & NECO", "price": "₦1,000", "tier_code": Subscription.Tier.WAEC_NECO},
        {"name": "University", "price": "₦2,000", "tier_code": Subscription.Tier.UNIVERSITY},
        {"name": "JAMB", "price": "₦3,000", "tier_code": Subscription.Tier.JAMB},
    ]
    return render(request, "landing.html", {"tiers": tiers})


@login_required
def dashboard_view(request):
    now = timezone.now()

    if request.user.is_superuser or request.user.is_staff:
        active_tiers = {Subscription.Tier.JAMB, Subscription.Tier.WAEC_NECO, Subscription.Tier.UNIVERSITY}
        unlocked_exam_types = [Subject.ExamType.JAMB, Subject.ExamType.WAEC, Subject.ExamType.NECO]
    else:
        active_subscriptions = Subscription.objects.filter(
            user=request.user,
            status=Subscription.Status.ACTIVE,
        )
        active_subscriptions = [
            s for s in active_subscriptions if s.end_date is None or s.end_date >= now
        ]
        active_tiers = {s.tier for s in active_subscriptions}

        exam_type_for_tier = {
            Subscription.Tier.JAMB: [Subject.ExamType.JAMB],
            Subscription.Tier.WAEC_NECO: [Subject.ExamType.WAEC, Subject.ExamType.NECO],
        }
        unlocked_exam_types = []
        for tier in active_tiers:
            unlocked_exam_types.extend(exam_type_for_tier.get(tier, []))

    available_subjects = (
        Subject.objects.filter(exam_type__in=unlocked_exam_types)
        if unlocked_exam_types else Subject.objects.all()
    )

    course_materials = (
        CourseMaterial.objects.filter(student=request.user)
        .prefetch_related("study_sessions")
        .order_by("-uploaded_at")[:10]
    )

    recent_attempts = (
        ExamAttempt.objects.filter(student=request.user)
        .select_related("subject")
        .order_by("-started_at")[:5]
    )

    is_new_signup = request.session.pop('is_new_signup', False)

    context = {
        "active_tiers": active_tiers,
        "available_subjects": available_subjects,
        "course_materials": course_materials,
        "recent_attempts": recent_attempts,
        "tier_choices": Subscription.Tier.choices,
        "is_new_signup": is_new_signup,
    }
    return render(request, "dashboard.html", context)


@login_required
def take_exam(request):
    subjects = Subject.objects.all()
    subject_id = request.GET.get('subject')
    
    if subject_id:
        return redirect('exam_page', subject_id=subject_id)

    context = {
        'subjects': subjects,
        'selected_subject': None,
        'questions': []
    }
    return render(request, 'exam.html', context)


@login_required
def exam_page_view(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)
    subjects = Subject.objects.all()
    
    if not (request.user.is_superuser or request.user.is_staff):
        tier_for_exam_type = {
            Subject.ExamType.JAMB: Subscription.Tier.JAMB,
            Subject.ExamType.WAEC: Subscription.Tier.WAEC_NECO,
            Subject.ExamType.NECO: Subscription.Tier.WAEC_NECO,
        }
        required_tier = tier_for_exam_type.get(subject.exam_type, Subscription.Tier.JAMB)

        has_access = Subscription.objects.filter(
            user=request.user,
            tier=required_tier,
            status=Subscription.Status.ACTIVE,
        ).exists()
        if not has_access:
            messages.error(request, "Active subscription required for this subject.")
            return redirect('dashboard')

    return render(request, "exam.html", {"subject": subject, "subjects": subjects})


# =========================================================================
# 4. EXAM-TAKING API VIEWS
# =========================================================================

@login_required
@require_POST
def start_exam_attempt(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)

    if not (request.user.is_superuser or request.user.is_staff):
        tier_for_exam_type = {
            Subject.ExamType.JAMB: Subscription.Tier.JAMB,
            Subject.ExamType.WAEC: Subscription.Tier.WAEC_NECO,
            Subject.ExamType.NECO: Subscription.Tier.WAEC_NECO,
        }
        required_tier = tier_for_exam_type.get(subject.exam_type, Subscription.Tier.JAMB)

        has_access = Subscription.objects.filter(
            user=request.user,
            tier=required_tier,
            status=Subscription.Status.ACTIVE,
        ).exists()
        if not has_access:
            return JsonResponse({"error": "Subscription required for this subject."}, status=403)

    QUESTIONS_PER_ATTEMPT = 40

    question_ids = list(
        Question.objects.filter(subject=subject)
        .order_by("?")
        .values_list("id", flat=True)[:QUESTIONS_PER_ATTEMPT]
    )

    if not question_ids:
        return JsonResponse({"error": "No questions available for this subject yet."}, status=404)

    attempt = ExamAttempt.objects.create(
        student=request.user,
        subject=subject,
        total_questions=len(question_ids),
    )

    return JsonResponse({
        "attempt_id": attempt.id,
        "subject": subject.name,
        "question_ids": question_ids,
        "total_questions": len(question_ids),
    })


@login_required
@require_GET
def get_question(request, attempt_id, question_id):
    attempt = get_object_or_404(ExamAttempt, id=attempt_id, student=request.user)

    question = get_object_or_404(
        Question.objects.select_related("subject").prefetch_related("options"),
        id=question_id,
        subject=attempt.subject,
    )

    options = [
        {"id": opt.id, "text": opt.option_text}
        for opt in question.options.all()
    ]

    return JsonResponse({
        "question_id": question.id,
        "question_text": question.question_text,
        "image_url": question.image.url if question.image else None,
        "options": options,
    })


def generate_ai_feedback(question, selected_option, is_correct) -> str:
    if is_correct:
        return f"Well done! You correctly identified '{selected_option.option_text}'. {question.explanation}"
    return (
        f"Not quite — you selected '{selected_option.option_text}', but let's "
        f"work through why. {question.explanation} Keep going, you're learning!"
    )


@login_required
@require_POST
def submit_answer(request, attempt_id):
    attempt = get_object_or_404(ExamAttempt, id=attempt_id, student=request.user)

    try:
        body = json.loads(request.body)
        question_id = int(body["question_id"])
        option_id = int(body["option_id"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return JsonResponse({"error": "Invalid request body."}, status=400)

    question = get_object_or_404(
        Question.objects.select_related("subject"),
        id=question_id,
        subject=attempt.subject,
    )

    selected_option = get_object_or_404(Option, id=option_id, question=question)
    is_correct = selected_option.is_correct

    with transaction.atomic():
        answer, created = StudentAnswer.objects.update_or_create(
            attempt=attempt,
            question=question,
            defaults={
                "selected_option": selected_option,
                "is_correct": is_correct,
            },
        )
        if created and is_correct:
            attempt.score = attempt.score + 1
            attempt.save(update_fields=["score"])

    ai_feedback = generate_ai_feedback(question, selected_option, is_correct)
    answer.ai_feedback = ai_feedback
    answer.save(update_fields=["ai_feedback"])

    correct_option_id = (
        question.options.filter(is_correct=True).values_list("id", flat=True).first()
    )

    return JsonResponse({
        "is_correct": is_correct,
        "correct_option_id": correct_option_id,
        "explanation": question.explanation,
        "ai_feedback": ai_feedback,
    })


@login_required
@require_GET
def get_attempt_results(request, attempt_id):
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("subject", "student"),
        id=attempt_id,
        student=request.user,
    )

    answers = (
        StudentAnswer.objects.filter(attempt=attempt)
        .select_related("question", "selected_option")
        .prefetch_related("question__options")
        .order_by("id")
    )

    results = []
    for ans in answers:
        correct_option = next((o for o in ans.question.options.all() if o.is_correct), None)
        results.append({
            "question_text": ans.question.question_text,
            "your_answer": ans.selected_option.option_text if ans.selected_option else None,
            "correct_answer": correct_option.option_text if correct_option else None,
            "is_correct": ans.is_correct,
            "ai_feedback": ans.ai_feedback,
        })

    return JsonResponse({
        "subject": attempt.subject.name,
        "score": attempt.score,
        "total_questions": attempt.total_questions,
        "results": results,
    })


# =========================================================================
# 5. UNIVERSITY TIER: PDF UPLOAD + STUDY SESSION (GEMINI POWERED)
# =========================================================================

ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".docx", ".txt"}
MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


def call_gemini_with_fallback(prompt):
    """
    Helper function to try multiple Gemini model endpoints sequentially
    if one encounters a 503 high demand or availability error.
    """
    models_to_try = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ]
    }

    last_data = {}
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={settings.GEMINI_API_KEY}"
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=25)
            data = response.json()
            
            if "candidates" in data and data["candidates"]:
                candidate = data["candidates"][0]
                content_parts = candidate.get("content", {}).get("parts", [])
                if content_parts:
                    text_out = content_parts[0].get("text", "")
                    if text_out:
                        return text_out
            last_data = data
        except Exception as e:
            logger.warning(f"Attempt with model {model_name} failed: {e}")
            continue

    error_msg = last_data.get("error", {}).get("message", "Service temporarily busy across available models.")
    return f"Gemini Error: {error_msg}"


def process_pdf_and_generate_study_session(material_id, course_outline=""):
    try:
        material = CourseMaterial.objects.get(id=material_id)
    except CourseMaterial.DoesNotExist:
        return

    file_path = material.file.path
    extracted_text = ""

    try:
        with open(file_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    extracted_text += text + "\n"
    except Exception as e:
        logger.error(f"Error reading file for material {material_id}: {e}")
        return

    extracted_text = extracted_text[:50000]

    try:
        prompt = (
            f"You are an expert AI professor and personal tutor for KDH (Knowledge Delivery Hub). "
            f"Analyze the following course material for '{material.course_title}'.\n\n"
            f"COURSE OUTLINE / SYLLABUS:\n{course_outline}\n\n"
            f"Course Material Text:\n{extracted_text}\n\n"
            "Using the uploaded course text and the syllabus above, please provide:\n"
            "1. A comprehensive study guide summary mapping directly to the syllabus topics.\n"
            "2. Practice test questions with options and explanations."
        )

        study_guide_content = call_gemini_with_fallback(prompt)

        sample_questions = [
            {
                "question": f"What is a primary concept emphasized in {material.course_title}?",
                "options": ["Core Principles", "Key Definitions", "Theoretical Applications", "All of the above"],
                "answer": "All of the above"
            }
        ]

        StudySession.objects.create(
            material=material,
            status="COMPLETED",
            study_guide=study_guide_content,
            generated_questions=sample_questions,
        )

    except Exception as e:
        logger.error(f"Error calling Gemini API for material {material_id}: {e}")
        StudySession.objects.create(
            material=material,
            status="FAILED",
            study_guide="Error generating AI study guide. Please try again.",
            generated_questions=[],
        )


@login_required
def upload_course_material(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get("file")
        course_title = request.POST.get("course_title", "").strip()
        course_outline = request.POST.get("course_outline", "").strip()

        if not uploaded_file:
            messages.error(request, "No file uploaded.")
            return redirect("dashboard")
        if not course_title:
            messages.error(request, "Course title is required.")
            return redirect("dashboard")

        ext = os.path.splitext(uploaded_file.name)[1].lower()
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            messages.error(request, f"Unsupported file type. Allowed: {', '.join(ALLOWED_UPLOAD_EXTENSIONS)}")
            return redirect("dashboard")
        if uploaded_file.size > MAX_UPLOAD_SIZE_BYTES:
            messages.error(request, "File too large (max 10MB).")
            return redirect("dashboard")

        material = CourseMaterial.objects.create(
            student=request.user,
            course_title=course_title,
            file=uploaded_file,
        )

        process_pdf_and_generate_study_session(material.id, course_outline)

        messages.success(request, "File received. Study session is being generated.")
        return redirect("dashboard")

    return render(request, "upload_material.html")


@login_required
@require_GET
def get_study_session(request, material_id):
    material = get_object_or_404(
        CourseMaterial.objects.prefetch_related("study_sessions"),
        id=material_id,
        student=request.user,
    )

    session = material.study_sessions.order_by("-created_at").first()
    if not session:
        return JsonResponse({"status": "PENDING", "message": "Still generating..."})

    return JsonResponse({
        "status": session.status,
        "study_guide": session.study_guide,
        "generated_questions": session.generated_questions,
    })


@login_required
@require_POST
def ask_study_assistant(request, material_id=None):
    try:
        body = json.loads(request.body)
        user_question = body.get("question", "").strip()
    except (json.JSONDecodeError, ValueError, TypeError):
        user_question = request.POST.get("question", "").strip()

    if not user_question:
        return JsonResponse({"error": "Question is required."}, status=400)

    try:
        if material_id:
            material = get_object_or_404(CourseMaterial, id=material_id, student=request.user)
            session = material.study_sessions.order_by("-created_at").first()
            study_context = session.study_guide if session else "No study guide generated yet."
            
            full_prompt = (
                "You are an expert AI tutor for KDH Study Space (Knowledge Delivery Hub). "
                f"Course: {material.course_title}\n"
                f"Study Guide Context:\n{study_context}\n\n"
                f"Student Question: {user_question}\n\n"
                "Provide a clear, educational, step-by-step academic explanation."
            )
        else:
            full_prompt = (
                "You are an expert AI tutor for KDH Study Space (Knowledge Delivery Hub). "
                f"Student Question: {user_question}\n\n"
                "Provide a helpful, concise, and encouraging academic response."
            )

        answer_text = call_gemini_with_fallback(full_prompt)
        return JsonResponse({"answer": answer_text})

    except Exception as e:
        logger.error(f"Error in ask_study_assistant with Gemini: {e}")
        return JsonResponse({"answer": f"Error generating answer: {str(e)}"}, status=500)


# =========================================================================
# 6. PAST QUESTIONS API INTEGRATION
# =========================================================================

@login_required
@require_GET
def fetch_external_past_questions(request):
    """
    Fetches real-time past questions from external providers (e.g., SdashAPI)
    for JAMB, WAEC, and NECO to feed the CBT frontend dynamically.
    """
    subject = request.GET.get('subject', 'mathematics')
    exam_type = request.GET.get('type', 'utme')
    year = request.GET.get('year', '2023')
    
    api_url = "https://sdashapi.com/api/v1/q"
    access_token = getattr(settings, 'SDASH_API_TOKEN', 'YOUR_ACCESS_TOKEN')
    
    headers = {
        "AccessToken": access_token
    }
    
    params = {
        "subject": subject,
        "type": exam_type,
        "year": year
    }
    
    try:
        response = requests.get(api_url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            return JsonResponse({"status": "success", "data": data.get("data", [])})
        else:
            logger.warning(f"External Past Questions API returned status {response.status_code}")
            return JsonResponse({"status": "error", "message": "Failed to fetch questions from provider."}, status=400)
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error connecting to Past Questions API: {e}")
        return JsonResponse({"status": "error", "message": "Network error reaching question bank."}, status=500)