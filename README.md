# Flow-HE

הכתבה קולית בעברית לכל אפליקציה ב-Windows, בסגנון Wispr Flow, על שירות התמלול שלך ב-Vercel (IVRIT).

## התקנה (נייד, בלי Python)
1. הורד את `Flow-HE-windows.zip` מ-Releases (או מ-Actions → האריזה האחרונה) וחלץ לתיקייה כלשהי.
2. הפעל `Flow-HE.exe` פעם אחת. ייווצרו לידו `.env`, `config.json`, `dictionary.json`, `snippets.json`.
3. ב-`.env` מלא את `TRANSCRIBE_TOKEN` (ואם תרצה ניקוי AI ו-Command Mode, גם `ANTHROPIC_API_KEY`). הפעל שוב.

## שימוש
- **הבר הצף**: 🎙 מתחיל הקלטה (מד עוצמה + טיימר), ■ מסיים ושולח, ✕ מבטל. גוררים אותו לכל מקום.
- **▾ לאן הטקסט הולך**: הדבקה בסמן / העתקה ללוח / חלון תוצאות עם היסטוריה.
- **מקשים**: Alt ימני לחוץ-והחזק · Ctrl+Alt+Space הפעל/עצור · Ctrl+Alt+C פקודה על טקסט מסומן · Ctrl+Alt+N פתק · Esc ביטול.
- **מילון** (`dictionary.json`): `replacements` = "אמור X → כתוב Y". **סניפטים** (`snippets.json`): ביטוי → טקסט מלא, גם בתוך משפט.
- **תמלול תוך כדי הכתבה**: ההקלטה רציפה; אחרי כל הפסקה של 2 שניות מה שנאמר עד אז נשלח לתמלול (החיתוך בתחילת השקט — שום מילה לא נחתכת, ואין חפיפה) ומופיע בשדה בזמן שאתה ממשיך לדבר. הטקסט החי הוא גם הסופי. ב-`config.json`: `chunk_silence_s` (2.0), `chunk_max_s` (28 — חיתוך בכוח אם אין הפסקה), `final_full_pass` (false; true = מעבר שני על כל ההקלטה בסוף).
- **ספירת סבבים** (תפריט ▾ או המגש): הקלטה אחת רציפה מהמיקרופון. אחרי כל שקט של 3 שניות מה שנאמר עד אז נשלח לתמלול ומופיע בחלון החי, וההקלטה ממשיכה. כל קטע נשלח עם שנייה מסוף הקטע הקודם, והחפיפה מנוקה לפי זמן (לא לפי טקסט — מספר שחוזר באמת נשאר). בסיום נשמר Word ללא מעבר נוסף. ב-`config.json`: `rounds_silence_s` (3.0), `rounds_overlap_s` (1.0), `rounds_max_s` (45 — חיתוך בכוח אם אין שקט).

## פיתוח
- `textproc.py` לוגיקת טקסט טהורה + `tests/` (pytest).
- כל push בונה EXE על Windows ב-GitHub Actions ומריץ self-test שמצלם את הבר בכל מצב (artifact `selftest-screenshots`).
  אם מוגדרים secrets `TRANSCRIBE_URL` ו-`TRANSCRIBE_TOKEN` והקובץ `tests/sample_he.wav` קיים, נבדק גם תמלול חי.
- tag `v*` מפרסם Release עם ה-ZIP.
- `replica/` מפת ה-recon של Wispr Flow ורשימת הפיצ'רים (`features.csv`).

## שרת התמלול
הקמת השרת (מודל העברית של ivrit.ai על RunPod + השרת ב-Vercel), הגדרות מומלצות, ביצועים ועלות: **[SERVER.md](SERVER.md)**
