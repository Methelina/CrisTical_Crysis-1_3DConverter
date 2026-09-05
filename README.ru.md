# CrisTical Crysis3D Converter

Конвертер игровых ассетов Crysis в glTF 2.0: персонажи, статичные и анимированные объекты, а также уровни. Поддерживаются Crysis 1 (оригинал), Warhead, Crysis 2, Crysis 3, Remastered и Wars; версия игры определяется автоматически по данным, выбирать её вручную в большинстве случаев не нужно.

Персонаж строится на основе `.cdf` (Character Definition File) — корневого файла сборки, который объединяет основную модель, все аттачменты и анимации со скелетом. Извлекает данные из бинарных .chr/.dba/.cga/.anm/.mtl файлов. Не зависит от сторонних конвертеров.

**Автор:** Soror L.'.L.'. aka Methelina&nbsp;|&nbsp; **Версия:** 2.1 &nbsp;|&nbsp; **Лицензия:** Apache 2.0

![Output](docs/action_motor.gif)

---

## Возможности

- **CDF-сборка** — автоматическое объединение Model + Attachments (CA_SKIN) в один glTF
- **Статичный CGF** — `cgf2gltf.py` читает геометрию без скелета (чанки Mesh/Node/DataStream/MeshSubsets) с сохранением **вершинных цветов → COLOR_0** и **тангентов → TANGENT** (распаковка упакованных int16-тангентов), иерархия нод запекается в мировое пространство
- **Скелет** — полная иерархия костей с корректными инверсными bind-матрицами
- **Меш** — все primitives с POSITION/NORMAL/UV/JOINTS/WEIGHTS
- **Анимации** — поддержка DBA v0903 (оригинал) и v0905 (Remaster)
- **Текстуры** — авто-конвертация DDS (DXT1/DXT5/ATI2N/3DC/RGBA8/L8) → PNG
- **DDN-нормали** — реконструкция Z-канала, DDNA gloss-экстракция по суффиксу
- **Emission** — альфа-канал Diffuse → emissiveTexture (emission power в Crysis)
- **DDS-Unsplit** — сборка split-файлов (.dds.0/.1/...) в единый DDS (mip-0), вдохновлен методом из [DDS-Unsplitter](https://github.com/Markemp/DDS-Unsplitter)
- **Материалы** — PBR metallicRoughness + baseColorTexture + normalTexture из .mtl
- **Мульти-материалы** — отдельные .mtl для каждого аттачмента
- **Разделение анимаций** — экспорт каждой в отдельный glTF
- **GLB-экспорт** — опциональный вывод одним бинарным файлом вместо .gltf+.bin
- **Quaternion fix** — устранение артефактов скручивания костей
- **GUI + CLI** — графическая панель управления и полноценный командный режим
- **Нативные диалоги** — кнопки Browse/+ открывают системный выбор файлов/папок (tkinter); встроенный диалог используется только как запасной вариант
- **Автоопределение папок** — GUI сам находит корень игры по структуре папок от .cdf
- **MCP-сервер** — `MCP_CrisTical_bridge.py` открывает весь пайплайн как нативные MCP-инструменты (Kilo Code, Claude, Cursor, ...): convert, scan, catalog, list, version, level2json, unpack
- **CGA-анимация** — анимированная геометрия (.cga) с иерархией нод и анимацией .anm → glTF
- **Коллайдер объекта** — опция `--extract-collision` выгружает движковый коллайдер в отдельный файл `<имя>_collision.gltf` (удобно для дверей, проёмов и арок)
- **Экспорт уровня** — `level2json.py` переводит уровень в читаемое JSON-описание (геометрия, объекты, свет; для данных Remastered — также полноценный цвет воксельных поверхностей)
- **Распаковка архивов** — `unpack_crysis.py` распаковывает зашифрованные .pak в обычные папки
- **Автоопределение редакции** — версия игры (Crysis 1/2/3, Warhead, Remastered, Wars) определяется автоматически по формату архивов
- **Универсальность** — работает с персонажами, объектами и уровнями Crysis 1–3, Warhead, Remastered и Wars

---

## Поддержка по версиям игры

| Возможность | Crysis 1 | Warhead | Crysis 2 | Crysis 3 | Remastered | Wars |
|------|------|------|------|------|------|------|
| Персонаж со скелетом и анимациями → glTF | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Статичный объект → glTF | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Анимированный объект (.cga) → glTF | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Текстуры + PBR-материалы | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Экспорт коллайдера объекта | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Экспорт уровня в JSON-описание | — | — | — | — | ✔ | — |
| Распаковка игровых архивов | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Полный цвет воксельных поверхностей | — | — | — | — | ✔ | — |

Легенда: ✔ = доступно · — = недоступно · «в работе» = частично/в разработке.

Warhead и Wars построены на том же движке и том же формате данных, что и Crysis 1, поэтому обрабатываются тем же способом, что и Crysis 1. Экспорт уровня в JSON и полный цвет воксельных поверхностей сейчас работают с данными Remastered.

---

## Системные требования

- **Windows 10/11 (64-бит)**
- **~2 ГБ свободного места на диске** (Python + пакеты + обработка данных игры)
- **Интернет при установке** — отдельно ставить Python не нужно, установщик сам подготавливает Python 3.11
- **tkinter** — поставляется вместе с подготовленным Python; используется GUI для нативных системных диалогов выбора файлов

---

## Быстрый старт

### Установка

Запустите `Install_CrisTical.bat` — однократно скачает и установит:

- **uv** — менеджер пакетов, через который подготавливается окружение
- **Python 3.11** — полноценная uv-managed сборка (включает **tkinter** для нативных системных диалогов выбора файлов в GUI)
- Python-библиотеки из `requirements.txt` (numpy, pillow, bpy, dearpygui, pygltflib + numba и cupy-cuda12x — бэкенды расшифровки Crysis 3 .pak + **mcp[cli]** для MCP-бриджа)
- Assimp 6.0.5 (assimp.dll) — для работы с 3D-форматами
- 7-Zip (7za.exe) — для извлечения .dba из Animations.pak
- **Проверка MCP-бриджа** — установка заверяется, что `scripts/MCP_CrisTical_bridge.py` импортируется без ошибок (FastMCP готов к работе)

Установщик **идемпотентен** — при повторном запуске уже установленные части
пропускаются, а venv пересоздаётся только если его нет или он устарел. Все кэши
загрузок лежат внутри проекта (`.cache/uv`), venv создаётся в `cris_env/`,
а сам интерпретатор Python подготавливается uv в профиле пользователя.
Интернет нужен только при установке.

### Запуск GUI

```batch
Run_CrisTical.bat
```

Открывается панель управления: выбор .cdf → автоскан модели → настройка анимаций/текстур → конвертация. Все действия логируются.

### CLI-режим

```batch
Run_CrisTical.bat --cdf alien.cdf --gamedir "F:\Games\Crysis\Game" --out output
Run_CrisTical.bat --cdf alien.cdf --gamedir "F:\Games\Crysis\Game" --split-anim --glb
```

### MCP-режим (нативные инструменты для AI-клиентов)

`scripts/MCP_CrisTical_bridge.py` — FastMCP stdio-сервер, который заменяет
`Run_CrisTical.bat` при работе через MCP: та же диспетчеризация, то же окружение
и тот же пайплайн, но в виде нативных инструментов с подробными отчётами.
Человек продолжает пользоваться `.bat`; AI-клиенты (Kilo Code, Claude, Cursor, ...)
работают через бридж.

| Инструмент | Описание |
|------|-------------|
| `cristical_convert` | Конвертация `.cdf`/`.chr`/`.cgf`/`.cga` → glTF/GLB; возвращает выполненную команду, полный лог пайплайна, код выхода, время работы и список созданных файлов |
| `cristical_scan` | Осмотр без конвертации: версии чанков, количество костей, статистика меша, материалы, анимации — файлы не записываются |
| `cristical_list` | Список файлов вывода с размерами и временем изменения |
| `cristical_version` | Отчёт об окружении: venv, скрипты, утилиты Bin/, версия mcp |
| `cristical_catalog` | Просмотр ассетов в игровых архивах по типу и пути (модели/анимации/текстуры/материалы) |
| `cristical_level2json` | Экспорт уровня в JSON-описание через тот же пайплайн, что и level2json.py |
| `cristorical_unpack` | Распаковка .pak в обычные папки (dry-run / rewrite / wait / status / crypto) |

Регистрация в Kilo Code (`kilo.json`, секция `mcp`):

```json
"cristical": {
  "type": "local",
  "command": ["K:\\work\\CrisTical_Crysis3DConverter\\cris_env\\Scripts\\python.exe",
              "K:\\work\\CrisTical_Crysis3DConverter\\scripts\\MCP_CrisTical_bridge.py"],
  "enabled": true,
  "timeout": 600000
}
```

---

## Интерфейс

![Интерфейс](docs/001_interface.png)

Панель управления показывает:

- **Статус CDF** — Valid (v0905) / Valid (v0903) / Invalid — версия контроллера анимаций
- **Статус Game Directories** — зелёный (папки валидны) / жёлтый (нет маркеров) / красный (не найдены)
- **Скан модели** — Bones, Primitives, Attachments, Materials, Animations
- **Автоопределение** — GUI сам поднимается по папкам от .cdf и находит корень игры
- **Нативные диалоги** — выбор файлов/папок через системный диалог (tkinter); встроенный диалог появляется только если tkinter недоступен
- **CLI-превью** — показывает формируемую команду для запуска из консоли
- **Редакция и опции** — автоопределение версии игры (Auto / Crysis 1 / Warhead / Crysis 2 / Crysis 3 / Remastered / Wars), режим текстур (Auto-PBR / Keep as-is / Skip), галочки «Output .glb» и «Extract collision mesh», вкладка «Map» для экспорта уровня в JSON-описание

---

# Статичная геометрия (.cgf)

Для объектов без скелета (растительность, пропсы, здания) используется `cgf2gltf.py`:

```batch
Run_CrisTical.bat --cgf palm_tree_large_a.cgf --gamedir "F:\Games\Crysis_Remastered\Game"
Run_CrisTical.bat --cgf bush.cgf --gamedir "F:\Games\Crysis\Game" --glb
```

Особенности:

- **Вершинные цвета сохраняются** — `COLOR_0` (RGBA 0..1). Это исходные RGBA-байты на вершину — та же конвенция, что используется в данных растительности Crysis для detail bending: R=жёсткость края листа, G=фаза листа, B=жёсткость ветки, A=ambient occlusion.
- **Тангенсы распаковываются** из упакованного формата (int16, f*32767).
- **Мульти-материалы** — `mat_id` субмеша резолвится через `subMaterials` материала ноды, с fallback по имени.
- Раскладка чанков определена самостоятельным анализом образцов файлов: Mesh 0xCCCC0000, Node 0xCCCC000B, MtlName 0xCCCC0014, DataStream 0xCCCC0016, MeshSubsets 0xCCCC0017.

---

## Зачем нужны папки игры (`--gamedir`)

Конвертер ищет три типа данных в указанных папках:

1. **Текстуры** — `.dds`/`.png` файлы по путям из `.mtl`. Ищутся в порядке указанных `--gamedir`.
2. **Анимации** — `.dba` файл по пути из `.cal` (`$TracksDatabase`). Так же ищется в `--gamedir`.
3. **Материалы** — `.mtl` файл рядом с `.cdf` или в папках игры.

### Как выбрать папки

Рекомендуемый порядок: **Remaster → оригинал → unpacked-контент**.

```batch
# Только Remaster (текстуры в PNG, анимации v0905)
--gamedir "F:\Games\Crysis_Remastered\Game"

# Remaster + оригинал (для старых .mtl/текстур, если в Remaster нет)
--gamedir "F:\Games\Crysis_Remastered\Game" --gamedir "F:\Games\Crysis\Game"

# + распакованный контент (split-DDS текстуры из .pak)
--gamedir "F:\Games\Crysis_Remastered\Game" --gamedir "F:\Games\Crysis\Game" --gamedir "F:\Games\Crysis_Remastered\__CONTENT\objectsch.pak_Unpacked"
```

**Правило:** первой ставится папка с наилучшими текстурами (Remaster — 4K PNG). Остальные — fallback для отсутствующих файлов.

---

## Флаги командной строки

| Флаг | Описание |
|------|----------|
| `--cdf <путь>` | Путь к `.cdf` или `.chr` файлу (анимированный персонаж) |
| `--cgf <путь>` | Путь к статичному `.cgf` файлу (растительность/пропсы, без скелета) |
| `--gamedir <папка>` | Корень игры (можно несколько, порядок = приоритет) |
| `--out <путь>` | Выходная папка (по умолчанию: `output/`) |
| `--no-anim` | Пропустить инжект анимаций |
| `--no-tex` | Пропустить конвертацию текстур |
| `--split-anim` | Экспорт каждой анимации в отдельный glTF |
| `--glb` | Вывод бинарным `.glb` вместо `.gltf`+`.bin` |
| `--cga <путь>` | Путь к анимированному `.cga` файлу |
| `--caf <путь>` | Инжект отдельного `.caf`-клипа поверх баз анимаций (можно несколько) |
| `--no-root-motion` | Убрать позиционный трек корневой кости из .caf-клипов |
| `--extract-collision` | Дополнительно выгрузить движковый коллайдер в `<имя>_collision.gltf` |
| `--level <путь>` | Экспорт уровня в JSON-описание (level2json) |
| `--help` | Показать справку |

---

## Результат

Все файлы записываются в выходную папку:

```
output/
├── model_name.gltf        # glTF 2.0 сцена (или .glb с флагом --glb)
├── model_name.bin         # Бинарный буфер (отсутствует в режиме --glb)
├── model_name.log         # Лог конвертации
├── material_diffuse.png   # Диффузная текстура
├── material_normal.png    # Normal-карта (Z-канал восстановлен)
├── material_emiss.png     # Emission-карта (из альфа-канала Diffuse)
├── material_specular.png  # Specular-карта
└── material_gloss.png     # Gloss-карта (DDNA alpha)
```

![Результат](docs/002_output.png)

С флагом `--split-anim` каждая анимация помещается в подпапку `model_name_anims/`.

---

## Форматы

| Файл | Формат | Версии |
|------|--------|--------|
| .cdf | Character Definition (XML) | Crysis 1–3, Remastered и др. |
| .chr | Бинарный формат персонажа Crysis (чанки) | v0744, v0745 |
| .cgf | Статичный формат Crysis (Mesh/Node/DataStream/MeshSubsets) | v0744, v0745 |
| .cga | Анимированная геометрия | v0744, v0745 |
| .anm | Анимация CGA (контроллеры TCB3) | — |
| .dba | База анимаций Crysis | v0903, v0905 |
| .caf | Одиночный анимационный клип | — |
| .chrparams | Настройки анимаций персонажа (XML) | — |
| .lmg | Группы локомоции (XML) | — |
| .bspace / .comb | Blend-space (XML) | — |
| .mtl | XML материал | одно-/много-материальный |
| .dds | DirectDraw Surface (split/combined) | DXT1, DXT5, ATI2N/3DC, RGBA8, L8 |
| .pak | Игровые архивы (zip/XXTEA/Twofish, зашифрованные) | C1/Remaster, C2, C3 |
| .xmlb | Бинарный CryXmlB/pbxml | C2/C3 |
| .cal | Character Animation List | текстовый |

---

## Структура проекта

```
CrisTical_Crysis3DConverter/
├── Install_CrisTical.bat          # Установщик окружения
├── Run_CrisTical.bat              # Лаунчер (GUI / CLI)
├── requirements.txt               # Python-зависимости (включая mcp[cli])
├── README.md / README.ru.md       # Документация
├── scripts/
│   ├── cristical_gui.py          # Панель управления (DearPyGui)
│   ├── cdf2gltf.py               # Оркестратор конвертации (персонажи .cdf/.chr)
│   ├── cgf2gltf.py               # Оркестратор конвертации (статичный .cgf)
│   ├── cga2gltf.py               # Оркестратор конвертации (анимированный .cga + .anm)
│   ├── level2json.py             # Экспорт уровня в JSON (в т.ч. воксели)
│   ├── unpack_crysis.py          # Распаковка игровых архивов .pak
│   ├── MCP_CrisTical_bridge.py   # MCP-сервер (FastMCP): convert/scan/catalog/list/version/level2json/unpack
│   └── cristical_core/           # Библиотека конвертера
│       ├── crychr.py              # Парсер .chr/.cdf (CompiledBones, DataStream, CDF XML)
│       ├── crycgf.py              # Парсер статичного .cgf (Mesh/Node/MtlName/DataStream/MeshSubsets, стримы COLOR0/COLOR1)
│       ├── crycga.py              # Парсер анимированного .cga
│       ├── crydba.py              # Парсер DBA v0903/v0905 (SmallTree64, Bitset, TCB)
│       ├── crycaf.py              # Парсер одиночных .caf-клипов
│       ├── crygltf.py             # glTF 2.0 writer (скелет + меш + статика + COLOR_0 + TANGENT)
│       ├── gltf_anim.py           # Инжектор анимаций + quaternion hemisphere fix
│       ├── crycollision.py        # Декодер движковых коллайдеров → <имя>_collision.gltf
│       ├── crychrparams.py / crylmg.py / crybspace.py / crytcb.py / crycodecs.py
│       ├── cryvfs.py / crypak.py / twofish_fast.py / pak_unpack.py / cryxmlb.py
│       ├── game_profile.py / mtl_resolve.py / path_resolve.py
│       └── ...                    # полный список модулей — в каталоге scripts/cristical_core/
├── resources/
│   └── ModeSevenBETAVHS.ttf      # Шрифт интерфейса
├── docs/                          # Скриншоты
├── .cache/                        # Кэш загрузок uv (установщик)
├── Bin/                           # Нативные утилиты (установщик)
├── cris_env/                      # Python venv (создаётся установщиком)
├── output/                        # Папка вывода
└── temp/                          # Временные файлы
```

---

## Благодарности

- [Khronos glTF 2.0 Specification](https://github.com/pygfx/gltflib)
- [BCnEncoder.NET](https://github.com/Nominom/BCnEncoder.NET) — BC decoding algorithms
- [DDS-Unsplitter](https://github.com/Markemp/DDS-Unsplitter) — reference split-DDS implementation
