"""
models.py
---------
Database models for the AI-Powered CBT & Exam Prep Platform.

Design overview:
1. CustomUser        -> Extends Django's built-in user (adds phone, etc.)
2. Subscription       -> Tracks which paid tier a user belongs to (JAMB, WAEC/NECO, University)
3. Subject / Question / Option -> Stores exam content for JAMB & WAEC/NECO
4. StudentAttempt / StudentAnswer -> Records what a student answered, for grading + AI feedback
5. CourseMaterial     -> Uploaded PDFs/outlines for university students
6. StudySession       -> AI-generated questions/answers/study guide from a CourseMaterial

Beginner notes are left as comments throughout — read them, they explain WHY,
not just WHAT.
"""

from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.conf import settings
from django.utils import timezone


# =========================================================================
# 1. CUSTOM USER MODEL
# =========================================================================
# Why: Django recommends starting every new project with a custom user model,
# even if it looks identical to the default one at first. If you ever need to
# add fields later (which you will), you won't have to painfully migrate your
# whole database. AbstractUser already gives you username, email, password,
# first_name, last_name, is_active, etc. for free — we just add a few extras.

# Nigerian phone validation: accepts 080/081/070/090/091-style local format
# (11 digits) or +234 international format. Rejecting garbage input here
# (server-side) matters because phone_number could otherwise be used for
# SMS-based OTP/notifications later — never trust unvalidated contact data.
phone_validator = RegexValidator(
    regex=r"^(\+234\d{10}|0\d{10})$",
    message="Enter a valid Nigerian phone number, e.g. 08012345678 or +2348012345678."
)


class CustomUser(AbstractUser):
    """Our project's user model. Everything else (Subscription, Attempts, etc.)
    points back to this.

    FIX FOR E304 (reverse accessor clash on groups/user_permissions):
    AbstractUser's built-in `groups` and `user_permissions` fields default
    to related_name="user_set" — the SAME name Django's own default
    `auth.User` model uses. If Django ever sees both in the same project
    (or the check runs against another AbstractUser subclass), it can't
    tell which model's `Group.user_set` / `Permission.user_set` a reverse
    lookup should refer to, so it raises E304/E305 and refuses to start.
    The fix: explicitly redeclare both fields here with a UNIQUE
    related_name so there is no ambiguity.
    """
    class Meta:
        app_label = 'models'
        
    # --- Redeclared to fix E304: unique related_name/related_query_name ---
    groups = models.ManyToManyField(
        "auth.Group",
        verbose_name="groups",
        blank=True,
        help_text="The groups this user belongs to.",
        related_name="customuser_set",        # was "user_set" (clash) -> now unique
        related_query_name="customuser",
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        verbose_name="user permissions",
        blank=True,
        help_text="Specific permissions for this user.",
        related_name="customuser_set",        # was "user_set" (clash) -> now unique
        related_query_name="customuser",
    )

    # --- Production hardening on top of AbstractUser's defaults ---

    # AbstractUser's `email` field is blank-allowed and NOT unique by
    # default, which is a real security/UX problem once email is used for
    # password resets or (as in payments.py) matching a Paystack customer
    # to a user. Enforce both here.
    email = models.EmailField(
        "email address",
        unique=True,
        blank=False,
    )

    phone_number = models.CharField(
        max_length=17,  # accommodates "+2348012345678" (14 chars) with headroom
        blank=True,
        validators=[phone_validator],
        help_text="Nigerian phone number, e.g. 08012345678"
    )

    # Some Nigerian students prep for university course-specific work.
    # This is optional metadata, not the subscription itself.
    school_name = models.CharField(max_length=255, blank=True)

    date_joined_platform = models.DateTimeField(default=timezone.now)

    # Use email as the login field instead of username, since it's now
    # guaranteed unique and is what your Paystack webhook already matches
    # on (see payments.py: User.objects.get(email=customer_email)).
    # username is still required by AbstractUser's schema, so we keep it
    # in REQUIRED_FIELDS to make sure it's still collected at signup.
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    # NOTE: no extra Meta.indexes needed for email — unique=True above
    # already creates a unique index at the database level, so a lookup
    # like User.objects.get(email=...) (used in payments.py's webhook)
    # is already fast without a redundant second index.

    def __str__(self):
        return self.username


# =========================================================================
# 2. SUBSCRIPTION / TIER MODEL
# =========================================================================
# Why a separate model instead of a field on CustomUser?
# - A user might renew, expire, or upgrade tiers over time — you want HISTORY.
# - Keeps payment/tier logic separate from authentication logic (clean design).

class Subscription(models.Model):
    """Tracks which paid tier a student currently has (or has had)."""

    class Tier(models.TextChoices):
        JAMB = "JAMB", "JAMB (₦3,000)"
        WAEC_NECO = "WAEC_NECO", "WAEC & NECO (₦1,000)"
        UNIVERSITY = "UNIVERSITY", "University (₦2,000)"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending Payment"
        ACTIVE = "ACTIVE", "Active"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions"
    )
    tier = models.CharField(max_length=20, choices=Tier.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )

    amount_paid = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    # Store the payment gateway's reference (Paystack/Flutterwave transaction ID).
    # Critical for verifying payment server-side — NEVER trust the frontend alone.
    payment_reference = models.CharField(max_length=100, blank=True, unique=True, null=True)

    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def is_active(self):
        """Helper to check in views/templates if this subscription currently works."""
        if self.status != self.Status.ACTIVE:
            return False
        if self.end_date and self.end_date < timezone.now():
            return False
        return True

    def __str__(self):
        return f"{self.user.username} - {self.tier} ({self.status})"


# =========================================================================
# 3. EXAM CONTENT MODELS (JAMB / WAEC / NECO)
# =========================================================================

class Subject(models.Model):
    """E.g. Mathematics, English, Physics, Government."""

    class ExamType(models.TextChoices):
        JAMB = "JAMB", "JAMB"
        WAEC = "WAEC", "WAEC"
        NECO = "NECO", "NECO"

    name = models.CharField(max_length=100)
    exam_type = models.CharField(max_length=10, choices=ExamType.choices)

    class Meta:
        # Prevents duplicate "Mathematics - JAMB" entries.
        unique_together = ("name", "exam_type")

    def __str__(self):
        return f"{self.name} ({self.exam_type})"


class Question(models.Model):
    """A single exam question belonging to a subject."""

    class Difficulty(models.TextChoices):
        EASY = "EASY", "Easy"
        MEDIUM = "MEDIUM", "Medium"
        HARD = "HARD", "Hard"

    subject = models.ForeignKey(
        Subject, on_delete=models.CASCADE, related_name="questions"
    )
    year = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Year this question is from, e.g. 2021 past question"
    )
    question_text = models.TextField()

    # Optional image for questions with diagrams (e.g. Physics, Geography).
    image = models.ImageField(upload_to="question_images/", blank=True, null=True)

    difficulty = models.CharField(
        max_length=10, choices=Difficulty.choices, default=Difficulty.MEDIUM
    )

    # This is where the "AI Teacher" explanation lives — shown AFTER the
    # student answers, so it can explain step-by-step rather than just
    # saying "correct" or "wrong."
    explanation = models.TextField(
        blank=True,
        help_text="Step-by-step explanation shown to the student after answering."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.subject.name}: {self.question_text[:50]}..."


class Option(models.Model):
    """One multiple-choice option (A, B, C, D) for a Question."""

    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="options"
    )
    option_text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        marker = "✓" if self.is_correct else "✗"
        return f"[{marker}] {self.option_text}"


# =========================================================================
# 4. STUDENT ATTEMPTS & ANSWERS
# =========================================================================
# Why separate Attempt vs Answer?
# - An "Attempt" = one full exam simulation session (start time, end time, score).
# - An "Answer" = one question's response within that attempt.
# This lets you show a full result breakdown, not just a final score.

class ExamAttempt(models.Model):
    """One full practice/exam session by a student."""

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="exam_attempts"
    )
    subject = models.ForeignKey(Subject, on_delete=models.SET_NULL, null=True)

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    score = models.PositiveIntegerField(default=0)
    total_questions = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.student.username} - {self.subject} - {self.score}/{self.total_questions}"


class StudentAnswer(models.Model):
    """A student's answer to one specific question within an ExamAttempt."""

    attempt = models.ForeignKey(
        ExamAttempt, on_delete=models.CASCADE, related_name="answers"
    )
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    selected_option = models.ForeignKey(
        Option, on_delete=models.SET_NULL, null=True, blank=True
    )
    is_correct = models.BooleanField(default=False)

    # Optional: store AI-generated encouragement/feedback text specific to
    # THIS answer (e.g. "You're close! You mixed up velocity and speed...").
    ai_feedback = models.TextField(blank=True)

    answered_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.attempt.student.username} - Q{self.question_id}"


# =========================================================================
# 5. UNIVERSITY TIER: COURSE MATERIALS + AI STUDY SESSIONS
# =========================================================================

class CourseMaterial(models.Model):
    """A PDF/course outline uploaded by a university student.
    The AI will read this and generate questions/study guides from it."""

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="course_materials"
    )
    course_title = models.CharField(max_length=255, help_text="E.g. 'CSC 201 - Data Structures'")

    # Beginner note: FileField (not ImageField) since this is a PDF/doc.
    file = models.FileField(upload_to="course_materials/%Y/%m/")

    # Extracted plain text from the PDF (filled in by your backend after
    # upload, e.g. using PyPDF2/pdfplumber), so the AI can read it as text
    # without re-parsing the PDF every single time.
    extracted_text = models.TextField(
        blank=True,
        help_text="Auto-filled: raw text extracted from the uploaded file."
    )

    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.course_title} ({self.student.username})"


class StudySession(models.Model):
    """One AI-generated study session (questions + answers + guide)
    produced from a CourseMaterial. Kept separate from CourseMaterial so
    a student can regenerate multiple sessions from the same material
    without losing earlier ones."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Generating..."
        COMPLETED = "COMPLETED", "Ready"
        FAILED = "FAILED", "Failed"

    material = models.ForeignKey(
        CourseMaterial, on_delete=models.CASCADE, related_name="study_sessions"
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="study_sessions"
    )

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )

    # The AI-generated study guide (explanations, summaries, key points).
    study_guide = models.TextField(blank=True)

    # Raw AI-generated Q&A, stored as JSON so it's flexible:
    # e.g. [{"question": "...", "options": [...], "answer": "...", "explanation": "..."}]
    generated_questions = models.JSONField(
        blank=True, null=True,
        help_text="AI-generated questions/answers stored as JSON."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"StudySession for {self.material.course_title} - {self.status}"
