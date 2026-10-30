# הקמת שרת התמלול (מודל עברית של ivrit.ai)

האפליקציה לא מתמללת על המחשב. היא שולחת אודיו לשרת קטן ב-**Vercel**, שמעביר אותו ל-GPU ב-**RunPod**, ושם רץ מודל העברית של **ivrit.ai**.
ככה המפתחות נשארים רק בשרת, והמחשב של המשתמש לא מתאמץ.

```
Flow-HE (Windows)  ──►  Vercel: server/  ──►  RunPod Serverless (GPU)  ──►  ivrit-ai/whisper-large-v3-turbo-ct2
   קוד הפעלה אישי        בודק קוד, מעביר            מתמלל (+ זיהוי דוברים)
```

## 1. המודל

- **מודל:** [`ivrit-ai/whisper-large-v3-turbo-ct2`](https://huggingface.co/ivrit-ai/whisper-large-v3-turbo-ct2) (Whisper large-v3-turbo שאומן על עברית, בפורמט CTranslate2 = faster-whisper).
- **שרת ההרצה הרשמי של ivrit.ai ל-RunPod:** האימג' `yairlifshitz/whisper-runpod-serverless:latest` (מתוך המאגר `ivrit-ai/runpod-serverless`). הוא מוריד את המודל לבד בהפעלה הראשונה.
- המלצה: להישאר עם האימג' הרשמי — כך מקבלים את התיקונים והמודלים החדשים של ivrit.ai בלי לתחזק Docker.

## 2. איפה להריץ — RunPod Serverless (מומלץ)

משלמים רק על שניות שבהן ה-GPU באמת עובד; בשקט — 0.

1. [runpod.io](https://www.runpod.io) → **Serverless** → **New Endpoint** → **Docker image**: `yairlifshitz/whisper-runpod-serverless:latest`
2. הגדרות שנבדקו אצלנו:

| הגדרה | ערך | למה |
| --- | --- | --- |
| GPU | 24 GB, דור חדש (בערך $0.75 לשעת עבודה) | מספיק בשפע: 12 שניות דיבור ≈ 0.5 שניות GPU; 134 שניות ≈ 2.6 שניות |
| Active workers | 0 | לא משלמים כשאף אחד לא מדבר |
| Max workers | 1 (2 אם כמה אנשים מדברים יחד הרבה) | כל worker נוסף עולה רק כשהוא עובד |
| Idle timeout | 15 שניות | אחרי 15 שניות שקט ה-GPU נכבה |
| FlashBoot | פועל | הפעלה מחדש מהירה (כ-2 שניות במקום כ-20) |
| Execution timeout | 1800 שניות | פגישות ארוכות |
| Container disk | 30 GB | המודל + האימג' |

3. **Settings → API Keys**: צרו מפתח מוגבל (Restricted) עם הרשאת קריאה/כתיבה רק ל-endpoint הזה.
4. רשמו את **Endpoint ID** ואת המפתח.

> GPU חזק יותר כמעט לא משפר: רוב ההמתנה היא הרשת וההמתנה להפסקה בדיבור, לא החישוב (ראו "ביצועים" למטה).

## 3. השרת ב-Vercel (התיקייה `server/`)

1. ב-Vercel: **Add New → Project** → בחרו את המאגר הזה → **Root Directory: `server`** → Deploy.
2. **Settings → Environment Variables** (Production):

| משתנה | ערך |
| --- | --- |
| `RUNPOD_ENDPOINT_ID` | ה-Endpoint ID מ-RunPod |
| `RUNPOD_API_KEY` | המפתח המוגבל מ-RunPod |
| `IVRIT_MODEL` | `ivrit-ai/whisper-large-v3-turbo-ct2` |
| `FLOWHE_TOKEN` | קוד ארוך ואקראי — הקוד שלכם (הבעלים) |
| `FLOWHE_USERS` | (לא חובה) קודים לאנשים נוספים: `שם:קוד;שם2:קוד2` — מוחקים זוג כדי לבטל גישה |
| `ANTHROPIC_API_KEY` | (לא חובה) לסיכומים דרך השרת |

3. אחרי שינוי משתנים — **Redeploy**.
4. בדיקה: `https://<הפרויקט>.vercel.app/api/health` צריך להחזיר `runpod_configured: true`.

קוד ארוך ואקראי אפשר ליצור כך: `python -c "import secrets; print(secrets.token_urlsafe(24))"`

## 4. חיבור האפליקציה

אחת משתי הדרכים:

- **קוד הפעלה:** בהפעלה הראשונה Flow-HE מבקש קוד — מדביקים קוד מ-`FLOWHE_USERS` (או `FLOWHE_TOKEN`).
- **קובץ:** ליד `Flow-HE.exe` יוצרים `server.env`:
  ```
  TRANSCRIBE_URL=https://<הפרויקט>.vercel.app/api/transcribe
  TRANSCRIBE_TOKEN=<הקוד>
  ```
- **חבילת התקנה מחוברת:** `tools/friend/` + קובץ `activation.txt` עם קוד → `install.bat` מתקין ומתחבר לבד.

אם השרת שלכם לא בכתובת `flow-he-api.vercel.app`, עדכנו את `DEFAULT_URL` ב-`flowhe.py` (או השתמשו ב-`server.env`).

## 5. ביצועים ועלות (נמדד)

| מצב | זמן |
| --- | --- |
| GPU על 12 שניות דיבור | ~0.5 שניות |
| GPU על 134 שניות | ~2.6 שניות |
| הלוך-חזור כשהשרת חם | ~1.2 שניות (רובו רשת) |
| worker במצב שינה (FlashBoot) | +~2 שניות |
| הפעלה קרה לגמרי | ~20 שניות (פעם ראשונה אחרי הרבה זמן) |

- האפליקציה מעירה את השרת כשלוחצים על מקש ההקלטה, ובפגישות שומרת אותו ער (עד 5 דקות בלי דיבור).
- עלות: משלמים רק על זמן GPU בפועל + 15 שניות אחרי הבקשה האחרונה. שימוש אישי רגיל — סנטים ליום.
