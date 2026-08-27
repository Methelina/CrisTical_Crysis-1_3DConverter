# CrisTical Crysis3D Converter

Конвертер анимированных персонажей Crysis 1 (оригинал + Remaster) в glTF 2.0.
Работает на основе `.cdf` (Character Definition File) — корневого файла сборки
персонажа, который объединяет основную модель, все аттачменты и анимации со скелетом.

Извлекает данные из бинарных .chr/.dba/.mtl файлов. Не зависит от сторонних конвертеров.

**Автор:** Soror L.'.L.'. aka Methelina &nbsp;|&nbsp; **Версия:** 2.1 &nbsp;|&nbsp; **Лицензия:** Apache 2.0

![Output](docs/action_motor.gif)

---

## Возможности

- **CDF-сборка** — автоматическое объединение Model + Attachments (CA_SKIN) в один glTF
- **Статичный CGF** — `cgf2gltf.py` читает геометрию без скелета (чанки Mesh/Node/DataStream/MeshSubsets) с сохранением **вершинных цветов → COLOR_0** и **тангентов → TANGENT** (распаковка int16 SMeshTangents), иерархия нод запекается в мировое пространство
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
- **Автоопределение папок** — GUI сам находит корень игры по структуре папок от .cdf
- **Универсальность** — работает с любыми персонажами и объектами Crysis 1

---

## Быстрый старт

### Установка

Запустите `Install_CrisTical.bat` — однократно скачает и установит:

- Python 3.11 с необходимыми библиотеками (pyassimp, numpy, pillow, bpy, dearpygui, trimesh, pygltflib)
- Assimp 6.0.5 (assimp.dll) — для работы с 3D-форматами
- 7-Zip (7za.exe) — для извлечения .dba из Animations.pak

Всё портативно, в пределах папки проекта. Интернет нужен только при установке.

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

---

## Интерфейс

![Интерфейс](docs/001_interface.png)

Панель управления показывает:

- **Статус CDF** — Valid (v0905) / Valid (v0903) / Invalid — версия контроллера анимаций
- **Статус Game Directories** — зелёный (папки валидны) / жёлтый (нет маркеров) / красный (не найдены)
- **Скан модели** — Bones, Primitives, Attachments, Materials, Animations
- **Автоопределение** — GUI сам поднимается по папкам от .cdf и находит корень игры
- **CLI-превью** — показывает формируемую команду для запуска из консоли

---

# Статичная геометрия (.cgf)

Для объектов без скелета (растительность, пропсы, здания) используется `cgf2gltf.py`:

```batch
Run_CrisTical.bat --cgf palm_tree_large_a.cgf --gamedir "F:\Games\Crysis_Remastered\Game"
Run_CrisTical.bat --cgf bush.cgf --gamedir "F:\Games\Crysis\Game" --glb
```

Особенности:

- **Вершинные цвета сохраняются** — `COLOR_0` (RGBA 0..1). Это те же байты `SMeshColor`, что использует шейдер растительности Crysis для detail bending: R=жёсткость края листа, G=фаза листа, B=жёсткость ветки, A=ambient occlusion.
- **Тангенсы распаковываются** из упакованного формата `Vec4sf` (int16, f*32767).
- **Мульти-материалы** — `mat_id` субмеша резолвится через `subMaterials` материала ноды (как в движке), с fallback по имени.
- Формат прочитан из исходников движка: `CGFLoader.cpp` / `CryHeaders.h` (чанки Mesh 0xCCCC0000, Node 0xCCCC000B, MtlName 0xCCCC0014, DataStream 0xCCCC0016, MeshSubsets 0xCCCC0017).

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
| .cdf | Character Definition (XML) | CryEngine 1–3 |
| .chr | CryTek chunk file | v0744, v0745 |
| .dba | CryAnimation Database | v0903, v0905 |
| .mtl | XML материал | одно-/много-материальный |
| .dds | DirectDraw Surface (split/combined) | DXT1, DXT5, ATI2N/3DC, RGBA8, L8 |
| .cal | Character Animation List | текстовый |

---

## Структура проекта

```
CrisTical_Crysis3DConverter/
├── Install_CrisTical.bat          # Установщик окружения
├── Run_CrisTical.bat              # Лаунчер (GUI / CLI)
├── README.md / README.ru.md       # Документация
├── scripts/
│   ├── cristical_gui.py          # Панель управления (DearPyGui)
│   ├── cdf2gltf.py               # Оркестратор конвертации (персонажи)
│   ├── cgf2gltf.py               # Оркестратор конвертации (статичный .cgf)
│   └── cristical_core/           # Библиотека конвертера
│       ├── crychr.py              # Парсер .chr/.cdf (CompiledBones, DataStream, CDF XML)
│       ├── crycgf.py              # Парсер статичного .cgf (Mesh/Node/MtlName/DataStream/MeshSubsets, стрим COLORS)
│       ├── crygltf.py             # glTF 2.0 writer (скелет + меш + статика + COLOR_0 + TANGENT)
│       ├── crydba.py              # Парсер DBA v0903/v0905 (SmallTree64, Bitset, TCB)
│       ├── gltf_anim.py           # Инжектор анимаций + quaternion hemisphere fix
│       ├── tex_convert.py         # Конвертер текстур (MTL→PNG, DDS→PNG, DDN Z, DDS unsplit)
│       ├── inject_anim.py         # CLI: инжект анимаций
│       └── convert_chr.py         # CLI: скелет + меш
├── resources/
│   └── ModeSevenBETAVHS.ttf      # Шрифт интерфейса
├── docs/                          # Скриншоты
├── Bin/                           # Нативные утилиты (установщик)
├── cris_env/                      # Python venv (установщик)
├── output/                        # Папка вывода
└── temp/                          # Временные файлы
```

---

## Благодарности

- [Khronos glTF 2.0 Specification](https://github.com/pygfx/gltflib)
- [BCnEncoder.NET](https://github.com/Nominom/BCnEncoder.NET) — BC decoding algorithms
- [DDS-Unsplitter](https://github.com/Markemp/DDS-Unsplitter) — reference split-DDS implementation
