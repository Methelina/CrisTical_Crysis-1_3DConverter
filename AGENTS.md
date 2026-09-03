# CrisTical — AGENTS.md (инструкции для агентов)

Проект: конвертер 3D-ассетов CryEngine (Crysis 1 / Warhead / Crysis 2 / Crysis 3 / Remastered) в glTF 2.0 / GLB. Никакого отношения к Unity и к «Alien Planet» нет — не используй упоминания других движков и проектов в инструкциях.

## Общая структура

| Что | Где |
| --- | --- |
| Код конвертера | `K:\work\CrisTical_Crysis3DConverter\scripts\` (+ пакет `scripts\cristical_core\`) |
| Модули форматов | `scripts\cristical_core\`: `crycaf.py`, `crydba.py`, `crycga.py`, `crycgf.py`, `crychr.py`, `crychrparams.py`, `crylmg.py`, `crygltf.py`, `cryvfs.py`, `game_profile.py`, `mtl_resolve.py` |
| Точки входа (CLI) | `scripts\cdf2gltf.py`, `scripts\cga2gltf.py`, `scripts\cgf2gltf.py` |
| Оркестратор (.bat) | `Run_CrisTical.bat` |
| Python-окружение | `cris_env` (venv) → `K:\work\CrisTical_Crysis3DConverter\cris_env\Scripts\python.exe` |
| Выходные файлы | `K:\work\CrisTical_Crysis3DConverter\output\<модель>\` |
| Документация/постановки | `K:\work\CrisTical_Crysis3DConverter\docs\internal\` (summary + task-файлы `.2do.md`) |
| MCP-сервер CrisTical | есть набор `cristical_*`-инструментов (convert/scan/catalog/unpack) — обёртки тех же скриптов |

## Корни данных игр (для `-g`/gamedir)

| Версия | Корень данных |
| --- | --- |
| C1 / Remaster | `F:\Games\Crysis_Remastered\Game` (оригинал C1: `F:\Games\Crysis\Game`) |
| C2 | `F:\Games\Crysis_2\gamecrysis2` |
| C3 | `F:\Games\Crysis_3\C3` |

Версия игры НЕ определяется по имени папки вручную: используй `cristical_core.game_profile.classify_game_dir` (паки: zip=Remaster/C1, xxtea=C2, twofish=C3).

## Как запускать конвертацию (принятый способ)

Всегда через `.bat`-оркестратор, с полным путём, двойными кавычками внутри двойных (PowerShell):

```
cmd /c 'K:\work\CrisTical_Crysis3DConverter\Run_CrisTical.bat --cdf "objects/characters/alien/grunt/grunt.cdf" -g "F:\Games\Crysis_2\gamecrysis2" -o "K:\work\CrisTical_Crysis3DConverter\output\grunt_c2\grunt.gltf"'
```

Форматы флагов: `--cdf` (персонаж: .chr/.cdf+анимации), `--cga` (объект CGA+ANM), `--cgf` (статичная геометрия). Путь к модели — виртуальный внутри паков геймдаты (не по диску!). Сборка идёт в `output\<имя>\`.

Быстрый прогон python-скрипта (если не нужен `.bat`): `& 'K:\work\CrisTical_Crysis3DConverter\cris_env\Scripts\python.exe' -m py_compile <файл>.py`.

## Реперные модели и их адреса

| Модель | Версия | Виртуальный .cdf | gamedir |
| --- | --- | --- | --- |
| grunt | C2 | `objects/characters/alien/grunt/grunt.cdf` | `F:\Games\Crysis_2\gamecrysis2` |
| mastermind | C3 | `Objects/characters/alien/mastermind/mastermind.cdf` | `F:\Games\Crysis_3\C3` |
| hunter | C1 | `objects/characters/alien/hunter/hunter.cdf` | `F:\Games\Crysis_Remastered\Game` |
| scout | C1 | `objects/characters/alien/scout/scout_base.cdf` | `F:\Games\Crysis_Remastered\Game` |
| tank (CGA) | C1 | `F:\Games\Crysis\Game\Objects\Vehicles\US_Tank\us_tank.cga` (loose, флаг `--cga`) | `F:\Games\Crysis\Game` |

## Ключевые правила и механики (соблюдать, не «улучшать» сломанное)

- Не менять код вне задачи; без рефакторинга «заодно».
- Строгая типизация, без неиспользуемого кода.
- Единицы CGA: вершины — как есть, ноды и TCB3-треки — cm→м (×0.01). Узловые сабсеты одного узла сливать в один glTF-меш.
- CA_SKIN-аттачменты ремапить на главный скелет по ИМЕНИ кости (порядок костей у .skin и .chr различается).
- CA_BONE `.cgf`-накладки: политика по версии через `game_profile` — C3 `raw` (модельно-пространственные), C2 `lift` (origin-детали: скининг весом 1 + перенос ТОЛЬКО по bind-переводу кости из `b2w[3],b2w[7],b2w[11]`, без поворота). Формула движка: мир накладки = `joint_anim * bind⁻¹ * AttAbsoluteDefault`. НЕ запекать поворот кости в вершины.
- Аддитивные клипы (`*_add`) писать как `bind ⊗ delta` (в `gltf_anim`); полный обход CAF по корню `.chrparams`; срезать md5-суффикс имён.
- Не создавать дубли резолверов версии — пользоваться `game_profile`.
- Авторство — единый авторитет `K:\work\CrisTical_Crysis3DConverter\docs\internal\author_canonical_label.json`. Никогда не печатать строку автора по памяти и не «исправлять» её — только прямая подстановка значений из файла. Скрипты автозамены/парсинга ника читают строку напрямую из JSON, без обработки её содержимого. Git-история НЕ авторитет. Сигил не «украшение»: каждая точка/апостроф значимы (`L.'.L'.` — ASCII-транслитерация `L∴L∴`), потеря или перестановка символов — ошибка.

## Документация и её правила

- Итоги/находки/постановки: `docs\internal\` (файлы `session-*.md`, `task-*.2do.md`). Новые задачи вести как отдельный `.2do.md` (самодостаточный, с полными дисковыми адресами, чтобы LLM могла начать без разведки).
- Язык общения и документации — русский; комментарии в коде — английский.
- Таблицы/каталоги в доках обязаны иметь читаемые `Описание`/`Примеры` (их читают гейм-дизайнеры), без «голых» идентификаторов.
- Запрещены жёсткие переносы строк внутри абзацев (каждый абзац — один непрерывный блок без переносов; переносы только между абзацами/списками/таблицами/заголовками).

## Проверка результата

- Численная проверка и headless Blender доступны: `bpy` установлен в `cris_env` (bpy 5.0). Импорт glTF и чтение мировых bbox — через скрипт на `cris_env` python.
- Визуальную проверку рендера (Game View / сцена) выполняет ТОЛЬКО пользователь в основной сессии. Агент готовит данные/сцену, но не «смотрит» скриншоты и не интерпретирует картинки.
- После правки кода: `python -m py_compile` изменённых файлов и пересборка реперной модели через `.bat`.
- Очищать промежуточные тестовые `.gltf/.bin/.log`; оставлять только канонические в `output\<модель>\`.
