"""Языки интерфейса. Ключ словаря — русская строка (родной язык программы).

Язык выбирается в «Настройках» (config.ui_lang); после смены программа предлагает
перезапуститься. Непереведённые строки остаются по-русски — словарь можно
дополнять без правки кода. Переводы подобраны по смыслу, а не дословно
(«Корзина» → Trash/Papierkorb/かご… как принято в соответствующих ОС и приложениях).
"""
from __future__ import annotations

# порядок = порядок в списке «Настроек»: белорусский стоит перед украинским
LANGS = {"ru": "Русский", "en": "English", "be": "Беларуская", "uk": "Українська",
         "pl": "Polski", "de": "Deutsch", "es": "Español", "fr": "Français",
         "it": "Italiano", "pt": "Português", "zh": "中文"}

CURRENT = "ru"

# ключ → (en, be, uk, pl, de, es, fr, it, pt, zh)
_K = {
"🏠 Обзор": ("Overview","Агляд","Огляд","Przegląd","Übersicht","Resumen","Aperçu","Panoramica","Visão geral","概览"),
"🗂 Карта": ("Map","Мапа","Мапа","Mapa","Karte","Mapa","Carte","Mappa","Mapa","地图"),
"🌳 Структура": ("Structure","Структура","Структура","Struktura","Struktur","Estructura","Structure","Struttura","Estrutura","结构"),
"✨ Промпт": ("Prompt","Промпт","Промпт","Prompt","Prompt","Prompt","Prompt","Prompt","Prompt","提示词"),
"🗑 Корзина": ("Trash","Кошык","Кошик","Kosz","Papierkorb","Papelera","Corbeille","Cestino","Cesto","废纸篓"),
"🧩 Плагины": ("Plugins","Плагіны","Плагіни","Wtyczki","Plugins","Complementos","Extensions","Estensioni","Complementos","插件"),
"📖 Инструкция": ("Guide","Інструкцыя","Інструкція","Instrukcja","Anleitung","Guía","Guide","Guida","Guia","指南"),
"⚙️ Настройки": ("Settings","Налады","Налаштування","Ustawienia","Einstellungen","Ajustes","Paramètres","Impostazioni","Definições","设置"),
"📜 Журнал": ("Log","Журнал","Журнал","Dziennik","Protokoll","Registro","Journal","Registro","Registo","日志"),
"🧹 Разбор завалов": ("Inbox zero","Разбор завалаў","Розбір завалів","Sortowanie zaległości","Inbox aufräumen","Ordenar pendientes","Vider les en attente","Sgombero pendenti","Organizar pendências","清理积压"),
"🧭 Ревизия структуры": ("Structure audit","Рэвізія структуры","Ревізія структури","Audyt struktury","Strukturprüfung","Auditoría de estructura","Audit de structure","Verifica struttura","Auditoria de estrutura","结构审查"),
"🔍 Вся система": ("Full system","Уся сістэма","Вся система","Cały system","Gesamtes System","Sistema completo","Système entier","Sistema intero","Sistema completo","全系统"),
"🪜 Система → потом разбор": ("System → then inbox","Сістэма → потым разбор","Система → потім розбір","System → potem sortowanie","System → dann Inbox","Sistema → luego ordenar","Système → puis tri","Sistema → poi smistamento","Sistema → depois ordenar","先系统后清理"),
"Файлов": ("Files","Файлаў","Файлів","Plików","Dateien","Archivos","Fichiers","File","Arquivos","文件"),
"Папок": ("Folders","Папак","Папок","Katalogów","Ordner","Carpetas","Dossiers","Cartelle","Pastas","文件夹"),
"К разбору": ("To sort","Да разбору","До розбору","Do posegregowania","Zu sortieren","Por ordenar","À trier","Da smistare","A organizar","待整理"),
"На проверке": ("In review","На праверцы","На перевірці","Do sprawdzenia","In Prüfung","En revisión","À vérifier","In verifica","Em revisão","待复核"),
"Отмечено": ("Selected","Адзначана","Позначено","Zaznaczone","Ausgewählt","Marcados","Sélectionnés","Selezionati","Marcados","已标记"),
"В истории": ("In history","У гісторыі","В історії","W historii","Im Protokoll","En historial","Dans l’historique","Nella cronologia","No histórico","已归档"),
"Режим работы": ("Working mode","Рэжым працы","Режим роботи","Tryb pracy","Arbeitsmodus","Modo de trabajo","Mode de travail","Modalità","Modo de trabalho","工作模式"),
"Рабочий цикл": ("Workflow","Працоўны цыкл","Робочий цикл","Cykl pracy","Arbeitszyklus","Ciclo de trabajo","Cycle de travail","Ciclo di lavoro","Ciclo de trabalho","工作流程"),
"Открыть результаты": ("Open results","Адкрыць вынікі","Відкрити результати","Otwórz wyniki","Ergebnisse öffnen","Abrir resultados","Ouvrir résultats","Apri risultati","Abrir resultados","打开结果"),
"▶ Начать анализ": ("▶ Start analysis","▶ Пачаць аналіз","▶ Почати аналіз","▶ Rozpocznij analizę","▶ Analyse starten","▶ Iniciar análisis","▶ Lancer l’analyse","▶ Avvia analisi","▶ Iniciar análise","▶ 开始分析"),
"⏸ Пауза": ("⏸ Pause","⏸ Паўза","⏸ Пауза","⏸ Pauza","⏸ Pause","⏸ Pausa","⏸ Pause","⏸ Pausa","⏸ Pausa","⏸ 暂停"),
"⏹ Стоп": ("⏹ Stop","⏹ Стоп","⏹ Стоп","⏹ Stop","⏹ Stopp","⏹ Parar","⏹ Stop","⏹ Stop","⏹ Parar","⏹ 停止"),
"💾 Сохранить": ("💾 Save","💾 Захаваць","💾 Зберегти","💾 Zapisz","💾 Speichern","💾 Guardar","💾 Enregistrer","💾 Salva","💾 Guardar","💾 保存"),
"💾 Сохранить настройки": ("💾 Save settings","💾 Захаваць налады","💾 Зберегти налаштування","💾 Zapisz ustawienia","💾 Einstellungen speichern","💾 Guardar ajustes","💾 Enregistrer les paramètres","💾 Salva impostazioni","💾 Guardar definições","💾 保存设置"),
"Диагностика": ("Diagnostics","Дыягностыка","Діагностика","Diagnostyka","Diagnose","Diagnóstico","Diagnostic","Diagnostica","Diagnóstico","诊断"),
"Открыть config.json": ("Open config.json","Адкрыць config.json","Відкрити config.json","Otwórz config.json","config.json öffnen","Abrir config.json","Ouvrir config.json","Apri config.json","Abrir config.json","打开 config.json"),
"📁 Пути": ("📁 Paths","📁 Шляхі","📁 Шляхи","📁 Ścieżki","📁 Pfade","📁 Rutas","📁 Chemins","📁 Percorsi","📁 Caminhos","📁 路径"),
"🤖 Модель и контекст": ("🤖 Model & context","🤖 Мадэль і кантэкст","🤖 Модель і контекст","🤖 Model i kontekst","🤖 Modell & Kontext","🤖 Modelo y contexto","🤖 Modèle et contexte","🤖 Modello e contesto","🤖 Modelo e contexto","🤖 模型与上下文"),
"🗺 Карта папок для модели": ("🗺 Folder map for the model","🗺 Мапа папак для мадэлі","🗺 Мапа папок для моделі","🗺 Mapa katalogów dla modelu","🗺 Ordnerkarte für das Modell","🗺 Mapa de carpetas para el modelo","🗺 Carte des dossiers pour le modèle","🗺 Mappa delle cartelle per il modello","🗺 Mapa de pastas para o modelo","🗺 模型用的文件夹地图"),
"⚖️ Решения и возможности": ("⚖️ Decisions & capabilities","⚖️ Рашэнні і магчымасці","⚖️ Рішення та можливості","⚖️ Decyzje i możliwości","⚖️ Entscheidungen & Funktionen","⚖️ Decisiones y funciones","⚖️ Décisions et fonctions","⚖️ Decisioni e funzioni","⚖️ Decisões e recursos","⚖️ 决策与功能"),
"Язык интерфейса (после перезапуска)": ("Interface language (after restart)","Мова інтэрфейсу (пасля перазапуску)","Мова інтерфейсу (після перезапуску)","Język interfejsu (po restarcie)","Oberflächensprache (nach Neustart)","Idioma (tras reiniciar)","Langue (après redémarrage)","Lingua (dopo riavvio)","Idioma (após reiniciar)","界面语言（重启后生效）"),
"правила раньше модели: однозначные случаи решаются без запроса (быстрее и стабильнее)":
  ("Rules before model: obvious cases resolve without an LLM call (faster, steadier)",
   "правілы перад мадэллю: адназначныя выпадкі вырашаюцца без запыту (хутчэй і стабільней)",
   "правила перед моделлю: однозначні випадки вирішуються без запиту (швидше і стабільніше)",
   "reguły przed modelem: oczywiste przypadki bez LLM (szybciej i stabilniej)",
   "Regeln vor dem Modell: eindeutige Fälle ohne LLM lösen (schneller, stabiler)",
   "reglas antes del modelo: casos evidentes sin LLM (más rápido y estable)",
   "règles avant le modèle : cas évidents traités sans LLM (plus rapide, plus stable)",
   "regole prima del modello: casi ovvi risolti senza LLM (più veloce e stabile)",
   "regras antes do modelo: casos óbvios sem LLM (mais rápido e estável)",
   "规则优先于模型：明确情形无需调用模型（更快更稳）"),
"двухпроходный разбор: сначала раздел → затем точная папка внутри него":
  ("two-pass analysis: section first → exact folder inside it",
   "двухпраходны разбор: спачатку раздзел → потым дакладная папка ўнутры яго",
   "двопрохідний розбір: спершу розділ → потім точна папка всередині нього",
   "analiza dwuetapowa: najpierw sekcja → potem dokładny folder",
   "Zwei-Durchlauf-Analyse: erst Bereich → dann genauer Ordner darin",
   "análisis en dos pasadas: primero sección → luego carpeta exacta",
   "analyse en deux passes : d’abord la section → puis le dossier exact",
   "analisi a due passaggi: prima la sezione → poi la cartella esatta",
   "análise em duas etapas: primeiro a seção → depois a pasta exata",
   "两段式分析：先选分区 → 再定精确文件夹"),
"↻ обновить всё": ("↻ refresh all","↻ абнавіць усё","↻ оновити все","↻ odśwież wszystko","↻ alles aktualisieren","↻ actualizar todo","↻ tout actualiser","↻ aggiorna tutto","↻ atualizar tudo","↻ 全部刷新"),
"Откатить последнюю пачку": ("Undo last batch","Скасаваць апошнюю пачку","Скасувати останню пачку","Cofnij ostatnią partię","Letzten Stapel zurücknehmen","Deshacer último lote","Annuler le dernier lot","Annulla ultimo lotto","Desfazer último lote","撤销最近一批"),
"🧹 Пустые папки": ("🧹 Empty folders","🧹 Пустыя папкі","🧹 Порожні папки","🧹 Puste katalogi","🧹 Leere Ordner","🧹 Carpetas vacías","🧹 Dossiers vides","🧹 Cartelle vuote","🧹 Pastas vazias","🧹 空文件夹"),
"🧲 Одноимённые папки — слить в канон": ("🧲 Same-name folders — merge into canonical","🧲 Аднайменныя папкі — зліць у канан","🧲 Однойменні папки — злити в канон","🧲 Katalogi o tej samej nazwie — scal","🧲 Gleichnamige Ordner — in Kanon zusammenführen","🧲 Carpetas homónimas — fusionar en canónica","🧲 Dossiers homonymes — fusionner dans la canonique","🧲 Cartelle omonime — unifica nella canonica","🧲 Pastas com mesmo nome — mesclar na canônica","🧲 同名文件夹 — 合并到规范目录"),
"🧭 Папки из «Разобрать» → постоянное место": ("🧭 «To sort» folders → permanent place","🧭 Папкі з «Разабраць» → пастаяннае месца","🧭 Папки з «Розібрати» → постійне місце","🧭 Katalogi «Do sortowania» → stałe miejsce","🧭 „Zu sortieren“-Ordner → fester Platz","🧭 Carpetas «Por ordenar» → lugar fijo","🧭 Dossiers «À trier» → emplacement définitif","🧭 Cartelle «Da smistare» → posto fisso","🧭 Pastas «A organizar» → local definitivo","🧭 “待整理”文件夹 → 固定位置"),
"не трогать": ("keep","не чапаць","не чіпати","nie ruszaj","behalten","no tocar","garder","lascia stare","não mexer","保留"),
"вернуть в список": ("return to list","вярнуць у спіс","повернути до списку","wróć do listy","zurück zur Liste","devolver a la lista","remettre dans la liste","riporta nell’elenco","devolver à lista","放回列表"),
"🗑 убрать помеченные в корзину Windows": ("🗑 move checked to Windows Recycle Bin","🗑 адзначаныя ў кошык Windows","🗑 позначені — у кошик Windows","🗑 zaznaczone do kosza Windows","🗑 markierte in den Windows-Papierkorb","🗑 marcadas a la papelera de Windows","🗑 sélectionnés vers la corbeille Windows","🗑 selezionate nel cestino di Windows","🗑 marcadas para a reciclagem do Windows","🗑 将勾选项移入回收站"),
"▶ слить помеченные": ("▶ merge checked","▶ зліць адзначаныя","▶ злити позначені","▶ scal zaznaczone","▶ markierte zusammenführen","▶ fusionar marcadas","▶ fusionner la sélection","▶ unifica selezionate","▶ mesclar selecionadas","▶ 合并勾选项"),
"опросить модель по всем папкам зоны": ("ask the model about all intake folders","апытаць мадэль па ўсіх папках зоны","запитати модель про всі папки зони","zapytaj model o wszystkie katalogi strefy","alle Ordner der Zone abfragen","consultar el modelo sobre todas las carpetas","interroger le modèle sur tous les dossiers","interroga il modello su tutte le cartelle","consultar o modelo sobre todas as pastas","询问模型所有待整理文件夹"),
"⏱ замерить скорость (текст + фото)": ("⏱ measure speed (text + photo)","⏱ вымераць хуткасць (тэкст + фота)","⏱ виміряти швидкість (текст + фото)","⏱ zmierz prędkość (tekst + zdjęcie)","⏱ Tempo messen (Text + Foto)","⏱ medir velocidad (texto + foto)","⏱ mesurer la vitesse (texte + photo)","⏱ misura velocità (testo + foto)","⏱ medir velocidade (texto + foto)","⏱ 测速（文本+图片）"),
"▶ перенести помеченные целиком": ("▶ move checked folders","▶ перанесці адзначаныя цалкам","▶ перенести позначені цілком","▶ przenieś zaznaczone w całości","▶ markierte ganz verschieben","▶ mover marcadas completas","▶ déplacer la sélection entière","▶ sposta intere le selezionate","▶ mover selecionadas inteiras","▶ 整体移动勾选项"),
"🌱 Недостающие папки (модель просит их снова и снова)": ("🌱 Missing folders (the model keeps asking for them)","🌱 Адсутныя папкі (мадэль просіць іх зноў і зноў)","🌱 Бракне папок (модель просить їх знову і знову)","🌱 Brakujące katalogi (model wciąż o nie prosi)","🌱 Fehlende Ordner (das Modell fordert sie immer wieder)","🌱 Carpetas que faltan (el modelo las pide una y otra vez)","🌱 Dossiers manquants (le modèle les demande sans cesse)","🌱 Cartelle mancanti (il modello le chiede di continuo)","🌱 Pastas ausentes (o modelo pede-as repetidamente)","🌱 缺失的文件夹（模型反复请求）"),
"▶ создать эти папки (только mkdir)": ("▶ create these folders (mkdir only)","▶ стварыць гэтыя папкі (толькі mkdir)","▶ створити ці папки (лише mkdir)","▶ utwórz te katalogi (tylko mkdir)","▶ diese Ordner anlegen (nur mkdir)","▶ crear estas carpetas (solo mkdir)","▶ créer ces dossiers (mkdir seul)","▶ crea queste cartelle (solo mkdir)","▶ criar estas pastas (só mkdir)","▶ 创建这些文件夹（仅 mkdir）"),
"📑 Аудит структуры": ("📑 Structure audit","📑 Аўдыт структуры","📑 Аудит структури","📑 Audyt struktury","📑 Struktur-Audit","📑 Auditoría de estructura","📑 Audit de structure","📑 Verifica struttura","📑 Auditoria de estrutura","📑 结构审计"),
"📂 Папки": ("📂 Folders","📂 Папкі","📂 Папки","📂 Katalogi","📂 Ordner","📂 Carpetas","📂 Dossiers","📂 Cartelle","📂 Pastas","📂 文件夹"),
"📄 План папок": ("📄 Folder plan","📄 План папак","📄 План папок","📄 Plan katalogów","📄 Ordnerplan","📄 Plan de carpetas","📄 Plan des dossiers","📄 Piano cartelle","📄 Plano de pastas","📄 文件夹计划"),
"Создать папки из плана": ("Create folders from plan","Стварыць папкі з плана","Створити папки з плану","Utwórz katalogi z planu","Ordner aus Plan anlegen","Crear carpetas del plan","Créer les dossiers du plan","Crea cartelle dal piano","Criar pastas do plano","按计划创建文件夹"),
"✅ Утвердить помеченные": ("✅ Approve selected","✅ Зацвердзіць адзначаныя","✅ Затвердити позначені","✅ Zatwierdź zaznaczone","✅ Ausgewählte genehmigen","✅ Aprobar marcadas","✅ Approuver la sélection","✅ Approva selezionate","✅ Aprovar marcadas","✅ 批准已标记"),
"👁 Смотреть и утвердить": ("👁 Review & approve","👁 Глядзець і зацвердзіць","👁 Дивитись і затвердити","👁 Przeglądaj i zatwierdź","👁 Prüfen & genehmigen","👁 Revisar y aprobar","👁 Examiner et approuver","👁 Rivedi e approva","👁 Rever e aprovar","👁 查看并批准"),
"Отметить": ("Select","Адзначыць","Позначити","Zaznacz","Auswählen","Marcar","Sélectionner","Seleziona","Marcar","标记"),
"Снять отметку": ("Deselect","Зняць адзначэнне","Зняти позначку","Odznacz","Abwählen","Desmarcar","Désélectionner","Deseleziona","Desmarcar","取消标记"),
"☑ Выделить все": ("☑ Select all","☑ Вылучыць усё","☑ Виділити все","☑ Zaznacz wszystko","☑ Alle auswählen","☑ Seleccionar todo","☑ Tout sélectionner","☑ Seleziona tutto","☑ Selecionar tudo","☑ 全选"),
"🔬 Перепроверить глубже": ("🔬 Recheck deeper","🔬 Пераправерыць глыбей","🔬 Перевірити глибше","🔬 Sprawdź dokładniej","🔬 Gründlicher prüfen","🔬 Revisar a fondo","🔬 Vérifier plus en profondeur","🔬 Ricontrolla più a fondo","🔬 Rever com mais cuidado","🔬 深度复核"),
"🩺 Проверить странное": ("🩺 Check oddities","🩺 Праверыць дзіўнае","🩺 Перевірити дивне","🩺 Sprawdź dziwne rzeczy","🩺 Auffälligkeiten prüfen","🩺 Comprobar lo extraño","🩺 Vérifier les anomalies","🩺 Controlla le anomalie","🩺 Verificar o estranho","🩺 检查异常项"),
"🧠 Уроки": ("🧠 Lessons","🧠 Урокі","🧠 Уроки","🧠 Lekcje","🧠 Lektionen","🧠 Lecciones","🧠 Leçons","🧠 Lezioni","🧠 Lições","🧠 经验"),
"🔁 Переразобрать выбранное": ("🔁 Reanalyze selected","🔁 Перазапрацаваць выбранае","🔁 Перерозібрати вибране","🔁 Przeanalizuj ponownie","🔁 Ausgewählte neu analysieren","🔁 Reanalizar selección","🔁 Réanalyser la sélection","🔁 Rianalizza selezionate","🔁 Reanalisar seleção","🔁 重新分析所选"),
"🧪 Пробный перенос": ("🧪 Dry-run move","🧪 Прабны перанос","🧪 Пробне переміщення","🧪 Test przeniesienia","🧪 Testlauf-Verschiebung","🧪 Simular traslado","🧪 Simulation de déplacement","🧪 Prova di spostamento","🧪 Simular transferência","🧪 模拟移动"),
"♻ Откатить пачку": ("♻ Undo batch","♻ Скасаваць пачку","♻ Скасувати партію","♻ Cofnij partię","♻ Stapel zurücknehmen","♻ Deshacer lote","♻ Annuler le lot","♻ Annulla lotto","♻ Desfazer lote","♻ 撤销批次"),
"🔄 Обновить": ("🔄 Refresh","🔄 Абнавіць","🔄 Оновити","🔄 Odśwież","🔄 Aktualisieren","🔄 Actualizar","🔄 Actualiser","🔄 Aggiorna","🔄 Atualizar","🔄 刷新"),
"📦 Папка целиком": ("📦 Whole folder","📦 Папка цалкам","📦 Папка цілком","📦 Cały katalog","📦 Ganzer Ordner","📦 Carpeta completa","📦 Dossier entier","📦 Cartella intera","📦 Pasta inteira","📦 整个文件夹"),
"Дубликаты": ("Duplicates","Дублікаты","Дублікати","Duplikaty","Duplikate","Duplicados","Doublons","Duplicati","Duplicados","重复项"),
"Похожие фото": ("Similar photos","Падобныя фота","Схожі фото","Podobne zdjęcia","Ähnliche Fotos","Fotos similares","Photos similaires","Foto simili","Fotos parecidas","相似照片"),
"Битые файлы": ("Broken files","Бітыя файлы","Биті файли","Uszkodzone pliki","Beschädigte Dateien","Archivos dañados","Fichiers corrompus","File danneggiate","Arquivos corrompidos","损坏文件"),
"Старые форматы: список": ("Old formats: list","Старыя фарматы: спіс","Старі формати: список","Stare formaty: lista","Alte Formate: Liste","Formatos antiguos: lista","Anciens formats : liste","Formati vecchi: elenco","Formatos antigos: lista","旧格式：列表"),
"Старые: конвертировать": ("Old formats: convert","Старыя: канвертаваць","Старі: конвертувати","Stare: konwertuj","Alte: konvertieren","Antiguos: convertir","Anciens : convertir","Vecchi: converti","Antigos: converter","旧格式：转换"),
"Имена: предложения": ("Names: suggestions","Імёны: прапановы","Імена: пропозиції","Nazwy: propozycje","Namen: Vorschläge","Nombres: propuestas","Noms : propositions","Nomi: suggerimenti","Nomes: sugestões","命名：建议"),
"Имена: применить": ("Names: apply","Імёны: прымяніць","Імена: застосувати","Nazwy: zastosuj","Namen: anwenden","Nombres: aplicar","Noms : appliquer","Nomi: applica","Nomes: aplicar","命名：应用"),
"🔄 Проверить": ("🔄 Verify","🔄 Праверыць","🔄 Перевірити","🔄 Sprawdź","🔄 Prüfen","🔄 Comprobar","🔄 Vérifier","🔄 Verifica","🔄 Verificar","🔄 检查"),
"🔎 Проверить промпт": ("🔎 Check prompt","🔎 Праверыць промпт","🔎 Перевірити промпт","🔎 Sprawdź prompt","🔎 Prompt prüfen","🔎 Comprobar prompt","🔎 Vérifier le prompt","🔎 Controlla prompt","🔎 Verificar prompt","🔎 检查提示词"),
# --- 1.1.0: новые подписи (пока только английский; остальные языки показывают русский)
"⋯ Ещё": ("⋯ More",),
"🚚 Применить перенос": ("🚚 Apply move",),
"📋 Копировать": ("📋 Copy",),
"📋 Копировать всё": ("📋 Copy all",),
"Закрыть": ("Close",),
"Развернуть всё": ("Expand all",),
"Свернуть": ("Collapse",),
"🧠 Модели Ollama": ("🧠 Ollama models",),
"🔄 Проверить Ollama": ("🔄 Check Ollama",),
"Взять как текстовую": ("Use as text model",),
"Взять для зрения": ("Use for vision",),
"✨ Подобрать лучшие": ("✨ Pick the best",),
"⬇ Скачать модель…": ("⬇ Download model…",),
"⬇ Скачать": ("⬇ Download",),
"Смотреть и утвердить": ("Review & approve",),
"✔ Не трогать": ("✔ Leave as is",),
"Пуск партии": ("Run batch","Запуск партыі","Запуск партії","Uruchom partię","Stapel starten","Iniciar lote","Lancer le lot","Avvia lotto","Iniciar lote","启动批次"),
}

_LANG_ORDER = ("en", "be", "uk", "pl", "de", "es", "fr", "it", "pt", "zh")
TR: dict[str, dict[str, str]] = {code: {} for code in _LANG_ORDER}
for _key, _vals in _K.items():
    for _code, _val in zip(_LANG_ORDER, _vals):
        TR[_code][_key] = _val


def set_lang(code: str) -> None:
    global CURRENT
    CURRENT = code if code in LANGS else "ru"


def lang() -> str:
    return CURRENT


def tr(text: str) -> str:
    if CURRENT == "ru" or not text:
        return text
    return TR.get(CURRENT, {}).get(text, text)
