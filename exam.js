/**
 * exam.js
 * -------
 * Drives the exam-taking interface: starts an attempt, loads questions
 * one at a time via fetch() (no full page reloads), runs a countdown
 * timer, and auto-submits the exam when time expires.
 *
 * SECURITY NOTE: this file NEVER receives or stores which option is
 * correct — that data is deliberately withheld by get_question (see
 * views.py) until AFTER an answer is submitted. Don't "helpfully" cache
 * correct answers here for a smoother UX; that would let any student
 * read them straight out of browser dev tools.
 */

(function () {
    const root = document.getElementById("exam-root");
    const EXAM_DURATION_SECONDS = 40 * 60; // 40 minutes — tune per subject if needed

    const startUrl = root.dataset.startUrl;
    const getQuestionTemplate = root.dataset.getQuestionUrlTemplate;
    const submitTemplate = root.dataset.submitUrlTemplate;
    const resultsTemplate = root.dataset.resultsUrlTemplate;

    let attemptId = null;
    let questionIds = [];
    let currentIndex = 0;
    let answeredMap = {};      // { questionId: optionId } — for nav dot styling
    let timerInterval = null;
    let secondsRemaining = EXAM_DURATION_SECONDS;
    let submitting = false;    // guards against double-submission race conditions

    const els = {
        timer: document.getElementById("timer"),
        loading: document.getElementById("loading-state"),
        card: document.getElementById("question-card"),
        counter: document.getElementById("question-counter"),
        text: document.getElementById("question-text"),
        options: document.getElementById("options-list"),
        feedback: document.getElementById("feedback-box"),
        nav: document.getElementById("question-nav"),
        prevBtn: document.getElementById("prev-btn"),
        nextBtn: document.getElementById("next-btn"),
        submitBtn: document.getElementById("submit-exam-btn"),
        resultsView: document.getElementById("results-view"),
        scoreSummary: document.getElementById("score-summary"),
        resultsList: document.getElementById("results-list"),
    };

    function getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(";").shift();
    }
    const csrftoken = getCookie("csrftoken");

    async function apiFetch(url, options = {}) {
        const res = await fetch(url, {
            ...options,
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrftoken,
                ...(options.headers || {}),
            },
        });
        if (!res.ok) {
            const errBody = await res.json().catch(() => ({}));
            throw new Error(errBody.error || `Request failed (${res.status})`);
        }
        return res.json();
    }

    // -----------------------------------------------------------------
    // 1. START THE EXAM
    // -----------------------------------------------------------------
    async function startExam() {
        try {
            const data = await apiFetch(startUrl, { method: "POST" });
            attemptId = data.attempt_id;
            questionIds = data.question_ids;

            renderNavDots();
            startTimer();
            await loadQuestion(0);

            els.loading.classList.add("hidden");
            els.card.classList.remove("hidden");
        } catch (err) {
            els.loading.textContent = `Could not start exam: ${err.message}`;
        }
    }

    // -----------------------------------------------------------------
    // 2. TIMER — counts down, auto-submits at zero
    // -----------------------------------------------------------------
    function startTimer() {
        updateTimerDisplay();
        timerInterval = setInterval(() => {
            secondsRemaining -= 1;
            updateTimerDisplay();

            // Visual urgency cue in the last 5 minutes.
            if (secondsRemaining === 300) {
                els.timer.classList.add("text-red-600", "animate-pulse");
            }

            if (secondsRemaining <= 0) {
                clearInterval(timerInterval);
                finishExam(/* autoSubmitted */ true);
            }
        }, 1000);
    }

    function updateTimerDisplay() {
        const m = Math.max(0, Math.floor(secondsRemaining / 60));
        const s = Math.max(0, secondsRemaining % 60);
        els.timer.textContent = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }

    // -----------------------------------------------------------------
    // 3. LOADING / SWITCHING QUESTIONS (no page reload)
    // -----------------------------------------------------------------
    async function loadQuestion(index) {
        currentIndex = index;
        const qId = questionIds[index];
        const url = getQuestionTemplate
            .replace("__ATTEMPT__", attemptId)
            .replace("__QID__", qId);

        els.feedback.classList.add("hidden");
        els.feedback.innerHTML = "";

        try {
            const q = await apiFetch(url, { method: "GET" });
            els.counter.textContent = `Question ${index + 1} of ${questionIds.length}`;
            els.text.textContent = q.question_text;

            els.options.innerHTML = "";
            q.options.forEach((opt) => {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.dataset.optionId = opt.id;
                btn.className =
                    "option-btn w-full text-left rounded-lg border border-stone-200 px-4 py-3 text-sm " +
                    "hover:border-forest-400 hover:bg-forest-50 transition";
                btn.textContent = opt.text;

                // If already answered, visually mark the previous selection.
                if (answeredMap[qId] === opt.id) {
                    btn.classList.add("border-forest-500", "bg-forest-50");
                }

                btn.addEventListener("click", () => submitAnswer(qId, opt.id, btn));
                els.options.appendChild(btn);
            });

            els.prevBtn.disabled = index === 0;
            const isLast = index === questionIds.length - 1;
            els.nextBtn.classList.toggle("hidden", isLast);
            els.submitBtn.classList.toggle("hidden", !isLast);

            updateNavDots();
        } catch (err) {
            els.text.textContent = `Error loading question: ${err.message}`;
        }
    }

    function renderNavDots() {
        els.nav.innerHTML = "";
        questionIds.forEach((qId, i) => {
            const dot = document.createElement("button");
            dot.type = "button";
            dot.dataset.index = i;
            dot.className = "nav-dot w-8 h-8 rounded-full text-xs font-medium border border-stone-300 bg-white text-stone-500";
            dot.textContent = i + 1;
            dot.addEventListener("click", () => loadQuestion(i));
            els.nav.appendChild(dot);
        });
    }

    function updateNavDots() {
        const dots = els.nav.querySelectorAll(".nav-dot");
        dots.forEach((dot, i) => {
            const qId = questionIds[i];
            dot.classList.remove("bg-forest-600", "text-white", "border-forest-600", "ring-2", "ring-forest-300");
            if (answeredMap[qId] !== undefined) {
                dot.classList.add("bg-forest-600", "text-white", "border-forest-600");
            }
            if (i === currentIndex) {
                dot.classList.add("ring-2", "ring-forest-300");
            }
        });
    }

    // -----------------------------------------------------------------
    // 4. SUBMITTING AN ANSWER (shows AI Teacher feedback immediately)
    // -----------------------------------------------------------------
    async function submitAnswer(questionId, optionId, clickedBtn) {
        // Disable all option buttons while the request is in flight, to
        // prevent a rapid double-click submitting two conflicting answers.
        els.options.querySelectorAll(".option-btn").forEach((b) => (b.disabled = true));

        const url = submitTemplate.replace("__ATTEMPT__", attemptId);
        try {
            const result = await apiFetch(url, {
                method: "POST",
                body: JSON.stringify({ question_id: questionId, option_id: optionId }),
            });

            answeredMap[questionId] = optionId;
            updateNavDots();

            // Highlight correct/incorrect option styling.
            els.options.querySelectorAll(".option-btn").forEach((b) => {
                const isSelected = Number(b.dataset.optionId) === optionId;
                const isCorrectOption = Number(b.dataset.optionId) === result.correct_option_id;
                if (isCorrectOption) {
                    b.classList.add("border-forest-500", "bg-forest-50", "text-forest-800");
                } else if (isSelected && !result.is_correct) {
                    b.classList.add("border-red-400", "bg-red-50", "text-red-700");
                }
            });

            // Show the AI Teacher's step-by-step feedback.
            els.feedback.classList.remove("hidden");
            els.feedback.className = `mt-5 rounded-lg p-4 text-sm ${
                result.is_correct ? "bg-forest-50 text-forest-800 border border-forest-200"
                                   : "bg-amber-50 text-amber-800 border border-amber-200"
            }`;
            els.feedback.innerHTML = `<p class="font-medium mb-1">${
                result.is_correct ? "✓ Correct!" : "Not quite — let's break it down"
            }</p><p>${result.ai_feedback}</p>`;
        } catch (err) {
            els.feedback.classList.remove("hidden");
            els.feedback.className = "mt-5 rounded-lg p-4 text-sm bg-red-50 text-red-700 border border-red-200";
            els.feedback.textContent = `Could not submit answer: ${err.message}`;
            els.options.querySelectorAll(".option-btn").forEach((b) => (b.disabled = false));
        }
    }

    // -----------------------------------------------------------------
    // 5. FINISHING THE EXAM (manual submit OR timer expiry)
    // -----------------------------------------------------------------
    async function finishExam(autoSubmitted = false) {
        if (submitting) return; // prevents double-fire if both the timer
        submitting = true;      // and a manual click race each other

        clearInterval(timerInterval);
        els.card.classList.add("hidden");
        document.getElementById("question-nav").classList.add("hidden");
        document.querySelector(".flex.items-center.justify-between:last-of-type")?.classList.add("hidden");

        const url = resultsTemplate.replace("__ATTEMPT__", attemptId);
        try {
            const data = await apiFetch(url, { method: "GET" });

            els.resultsView.classList.remove("hidden");
            els.scoreSummary.textContent = autoSubmitted
                ? `Time's up! You scored ${data.score} out of ${data.total_questions} in ${data.subject}.`
                : `You scored ${data.score} out of ${data.total_questions} in ${data.subject}.`;

            els.resultsList.innerHTML = "";
            data.results.forEach((r, i) => {
                const div = document.createElement("div");
                div.className = `rounded-lg border p-4 text-sm ${
                    r.is_correct ? "border-forest-200 bg-forest-50" : "border-red-200 bg-red-50"
                }`;
                div.innerHTML = `
                    <p class="font-medium text-stone-800 mb-1">${i + 1}. ${r.question_text}</p>
                    <p class="text-stone-600">Your answer: ${r.your_answer ?? "(skipped)"}</p>
                    <p class="text-stone-600">Correct answer: ${r.correct_answer}</p>
                `;
                els.resultsList.appendChild(div);
            });
        } catch (err) {
            els.resultsView.classList.remove("hidden");
            els.scoreSummary.textContent = `Could not load results: ${err.message}`;
        }
    }

    // -----------------------------------------------------------------
    // Wire up navigation buttons + boot
    // -----------------------------------------------------------------
    els.prevBtn.addEventListener("click", () => loadQuestion(currentIndex - 1));
    els.nextBtn.addEventListener("click", () => loadQuestion(currentIndex + 1));
    els.submitBtn.addEventListener("click", () => {
        if (confirm("Submit your exam now? You won't be able to change answers after this.")) {
            finishExam(false);
        }
    });

    // Warn on accidental tab close mid-exam (doesn't block timer auto-submit,
    // only fires on actual navigation/close attempts).
    window.addEventListener("beforeunload", (e) => {
        if (!submitting && attemptId) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    startExam();
})();