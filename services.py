import requests
from django.conf import settings
from .models import Subject, Question, Option

def fetch_and_save_past_questions(subject_slug, exam_type, year=None, limit=10):
    url = "https://www.sdashapi.com/api/v1/q"
    
    # Supporting both common token header styles just in case
    headers = {
        "AccessToken": getattr(settings, 'SDASH_API_TOKEN', ''),
        "Authorization": f"Bearer {getattr(settings, 'SDASH_API_TOKEN', '')}"
    }
    
    params = {
        "subject": subject_slug,
        "type": exam_type.lower(),  # e.g., 'utme', 'wassce', 'neco'
        "limit": limit
    }
    if year:
        params["year"] = year

    try:
        response = requests.get(url, headers=headers, params=params)
        
        # Print status and response text to debug easily in your terminal
        print(f"API Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()

            if isinstance(data, list):
                questions_list = data
            elif isinstance(data, dict):
                questions_list = data.get("data", data.get("results", data.get("questions", [])))
            else:
                questions_list = []

            if not questions_list:
                print("Warning: Response returned 200 OK, but no questions found in JSON payload:", data)
                return False

            subject_obj, _ = Subject.objects.get_or_create(
                name=subject_slug.capitalize(),
                exam_type=exam_type.upper()
            )

            for q_data in questions_list:
                question_text = q_data.get("question")
                q_year = q_data.get("examyear", year) or 2024  # fallback year if missing
                solution = q_data.get("solution", "")
                correct_answer = str(q_data.get("answer", "")).lower()

                if not question_text:
                    continue

                question, created = Question.objects.get_or_create(
                    subject=subject_obj,
                    question_text=question_text,
                    defaults={
                        "year": q_year,
                        "explanation": solution
                    }
                )

                if created:
                    options_dict = q_data.get("option", {})
                    for key, text in options_dict.items():
                        if text:
                            Option.objects.create(
                                question=question,
                                option_text=text,
                                is_correct=(str(key).lower() == correct_answer)
                            )
            return True
        else:
            print(f"API Error Response: {response.text}")
            
    except Exception as e:
        print("GEMINI ERROR DETAIL:", e)
        
    return False