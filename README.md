# SFTP Studio — Tauri Windows Package

هذا المشروع يحول أداة SFTP الحالية إلى حزمة Tauri بواجهة Dashboard جديدة مستوحاة من مرجع التصميم المختصر في docs/ui-kit-reference.md، مع الحفاظ على محرك Python القديم كطبقة توافق حتى تبقى كل وظائف التنزيل وM3U وMavin وCobra وFFmpeg وFirefox وSSH SOCKS5 متاحة.

## التشغيل من GitHub

1. أنشئ مستودعًا جديدًا على GitHub.
2. ارفع محتويات هذا المجلد بالكامل إلى المستودع.
3. افتح تبويب **Actions**.
4. شغّل workflow باسم **Build SFTP Studio for Windows**.
5. بعد انتهاء البناء حمّل artifact باسم `sftp-studio-windows`.

يمكن أيضًا إنشاء tag مثل `v1.0.0` لتشغيل البناء تلقائيًا.

## تشغيل محلي

يتطلب التشغيل المحلي Node.js وRust وPython. على Windows:

```powershell
npm install
pip install -r legacy/requirements.txt
npm run tauri dev
```

## توافق config.json

ضع نسخة الإعدادات القديمة في مجلد بيانات التطبيق أو استوردها من إعدادات التطبيق. الكود القديم يتعامل مع schema القديمة لمسارات M3U ويحوّلها إلى schema 2 مع إبقاء المفاتيح القديمة للتوافق.

لا ترفع `config.json` الحقيقي إلى GitHub؛ الملف مستثنى في `.gitignore`. استخدم `config.example.json` كنموذج.

## ملاحظات البناء

- لا يحتاج جهاز المستخدم إلى تثبيت Rust أو Tauri عند تنزيل ملف Windows الناتج.
- يتم بناء محرك Python القديم بواسطة PyInstaller داخل GitHub Actions ثم تضمينه كـ sidecar داخل حزمة Tauri.
- الواجهة الجديدة خفيفة، بينما زر «فتح المحرك القديم» يضمن الوصول إلى جميع الوظائف الحالية دون فقد بيانات أو إعدادات.


## معاينة الواجهة

يمكن تشغيل المعاينة محليًا من مجلد `src` بواسطة خادم ملفات ثابت. هذه المعاينة تفاعلية وتسمح بالتنقل بين الأقسام ومحاكاة حفظ الإعدادات واختبار الاتصال وإضافة رابط تنزيل تجريبي.

## ملاحظة بناء Tauri

يجب أن يكون الملف التنفيذي الرئيسي في `src-tauri/src/main.rs`، مع تعريف هدف `sftp-studio` داخل `src-tauri/Cargo.toml`. هذا ضروري حتى ينشئ Tauri ملف `src-tauri/target/release/sftp-studio.exe` قبل التغليف.
