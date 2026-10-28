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

## פיתוח
- `textproc.py` לוגיקת טקסט טהורה + `tests/` (pytest).
- כל push בונה EXE על Windows ב-GitHub Actions ומריץ self-test שמצלם את הבר בכל מצב (artifact `selftest-screenshots`).
  אם מוגדרים secrets `TRANSCRIBE_URL` ו-`TRANSCRIBE_TOKEN` והקובץ `tests/sample_he.wav` קיים, נבדק גם תמלול חי.
- tag `v*` מפרסם Release עם ה-ZIP.
- `replica/` מפת ה-recon של Wispr Flow ורשימת הפיצ'רים (`features.csv`).
