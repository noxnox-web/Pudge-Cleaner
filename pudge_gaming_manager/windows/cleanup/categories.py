"""The catalogue: every kind of data the cleaner may remove.

Split out of ``rules.py`` so the guards and the list of things they guard
stay separately reviewable. Adding an entry here cannot loosen a guard —
the engine applies every layer in ``rules`` to whatever a category names.

Browser caches address profile directories by wildcard (``User Data\\*``)
because a PC can have several Chrome profiles and Firefox names its profile
directories randomly. Each entry names the *cache* subdirectories only:
cookies, saved logins and history are deliberately absent, so a player is
never signed out by a cleanup (rule #46).
"""

from __future__ import annotations

from .rules import CleanupCategory, CleanupRisk

#: Chromium-family browsers all keep their caches under the same three
#: subdirectory names within a profile, so one helper builds each entry.
_CHROMIUM_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache")


def _chromium(
    category_id: str, name: str, vendor_root: str, *, profiles: str = "User Data\\*"
) -> CleanupCategory:
    """A cache category for one Chromium-based browser.

    Args:
        vendor_root: Template for the browser's data directory.
        profiles: Relative glob selecting profile directories beneath it.
            Chromium browsers use ``User Data\\<profile>``; Opera keeps its
            profile at the vendor root itself, so it passes ``''``.
    """
    prefix = f"{vendor_root}\\{profiles}" if profiles else vendor_root
    return CleanupCategory(
        id=category_id,
        name=f"Кэш {name}",
        description=f"Кэш страниц и шейдеров {name}. Не куки, не пароли, не история.",
        rationale=(
            "Кэшированные ресурсы страниц, которые браузер скачивает заново "
            "при следующем посещении. Куки, сохранённые входы и история не "
            "затрагиваются, поэтому из аккаунтов никого не выкинет."
        ),
        roots=tuple(f"{prefix}\\{sub}" for sub in _CHROMIUM_CACHE_DIRS),
        min_age_hours=0.0,
        risk=CleanupRisk.LOW,
    )


CATEGORIES: tuple[CleanupCategory, ...] = (
    CleanupCategory(
        id="temp.user",
        name="Временные файлы пользователя",
        description="Временные файлы, созданные приложениями для этого пользователя.",
        rationale=(
            "Windows и приложения пишут сюда черновые данные и должны сами "
            "за собой убирать; многое остаётся после аварийных завершений. "
            "Всё ещё нужное создаётся заново по требованию."
        ),
        roots=("%TEMP%", "%TMP%"),
        min_age_hours=24.0,
        risk=CleanupRisk.SAFE,
    ),
    CleanupCategory(
        id="temp.windows",
        name="Временные файлы Windows",
        description="Общесистемная папка черновых данных, используемая установщиками.",
        rationale=(
            "Рабочее место установщиков и обслуживания системы. Записи "
            "старше суток относятся к уже завершённым операциям."
        ),
        roots=("%SystemRoot%\\Temp",),
        min_age_hours=24.0,
        risk=CleanupRisk.SAFE,
        requires_admin=True,
    ),
    CleanupCategory(
        id="cache.shader.directx",
        name="Кэш шейдеров DirectX",
        description="Скомпилированный кэш шейдеров Direct3D.",
        rationale=(
            "Восстанавливается драйвером автоматически. Очистка стоит "
            "нескольких секунд подтормаживаний при первом запуске игры и "
            "лечит падения из-за повреждённого кэша после обновления "
            "драйвера."
        ),
        roots=("%LOCALAPPDATA%\\D3DSCache",),
        min_age_hours=0.0,
        risk=CleanupRisk.LOW,
    ),
    CleanupCategory(
        id="cache.shader.nvidia",
        name="Кэш шейдеров NVIDIA",
        description="Кэши шейдеров NVIDIA для DirectX и OpenGL.",
        rationale=(
            "Создаётся драйвером заново. Устаревший кэш после обновления "
            "драйвера — частая причина падений при первом запуске."
        ),
        roots=(
            "%LOCALAPPDATA%\\NVIDIA\\DXCache",
            "%LOCALAPPDATA%\\NVIDIA\\GLCache",
            "%LOCALAPPDATA%\\NVIDIA Corporation\\NV_Cache",
        ),
        min_age_hours=0.0,
        risk=CleanupRisk.LOW,
    ),
    CleanupCategory(
        id="cache.thumbnails",
        name="Кэш эскизов",
        description="Файлы базы эскизов Проводника.",
        rationale=(
            "Проводник создаёт эскизы заново по мере надобности. Очистка "
            "убирает накопившиеся устаревшие и неправильные превью."
        ),
        roots=("%LOCALAPPDATA%\\Microsoft\\Windows\\Explorer",),
        patterns=("thumbcache_*.db", "iconcache_*.db"),
        recursive=False,
        min_age_hours=0.0,
        risk=CleanupRisk.SAFE,
    ),
    CleanupCategory(
        id="dumps.crash",
        name="Дампы сбоев",
        description="Дампы аварийных завершений и отчёты об ошибках Windows.",
        rationale=(
            "Диагностические снимки прошлых сбоев. Они нужны только пока "
            "этот сбой активно разбирают, и бывают очень большими."
        ),
        roots=(
            "%LOCALAPPDATA%\\CrashDumps",
            "%LOCALAPPDATA%\\Microsoft\\Windows\\WER\\ReportArchive",
            "%LOCALAPPDATA%\\Microsoft\\Windows\\WER\\ReportQueue",
        ),
        min_age_hours=24.0,
        risk=CleanupRisk.LOW,
    ),
    CleanupCategory(
        id="cache.delivery_optimization",
        name="Кэш оптимизации доставки",
        description="Одноранговые данные обновлений, кэшированные для других ПК.",
        rationale=(
            "Windows держит здесь содержимое обновлений, чтобы раздавать "
            "его другим машинам в локальной сети. Наполняется заново по "
            "мере надобности и в клубной сети дорастает до нескольких "
            "гигабайт."
        ),
        roots=("%SystemRoot%\\SoftwareDistribution\\DeliveryOptimization",),
        min_age_hours=24.0,
        risk=CleanupRisk.LOW,
        requires_admin=True,
    ),
    CleanupCategory(
        id="cache.windows_update_download",
        name="Кэш загрузок Windows Update",
        description=(
            "Скачанные пакеты обновлений старше недели из "
            "SoftwareDistribution\\Download."
        ),
        rationale=(
            "Windows Update складывает сюда скачанные пакеты и не всегда "
            "убирает их после установки. Очистка этой папки — штатный шаг "
            "Microsoft при неполадках обновления: всё, что ещё понадобится, "
            "служба скачает заново. Берутся только файлы старше недели, а "
            "занятые установкой пропускаются, поэтому идущее обновление не "
            "прерывается. Выключено по умолчанию: цена ошибки — повторная "
            "загрузка обновления, и решать это должен оператор."
        ),
        roots=("%SystemRoot%\\SoftwareDistribution\\Download",),
        min_age_hours=168.0,  # one week
        risk=CleanupRisk.LOW,
        requires_admin=True,
        enabled_by_default=False,
    ),
    CleanupCategory(
        id="logs.windows",
        name="Журналы Windows",
        description="Журналы обслуживания и установки.",
        rationale=(
            "Текстовые журналы завершённых операций обслуживания Windows. "
            "Их читают только при разборе неудачного обновления."
        ),
        roots=("%SystemRoot%\\Logs\\CBS", "%SystemRoot%\\Logs\\DISM"),
        patterns=("*.log", "*.cab", "*.etl"),
        min_age_hours=168.0,  # one week
        risk=CleanupRisk.LOW,
        requires_admin=True,
        enabled_by_default=False,
    ),
    # -- browser caches ----------------------------------------------------
    _chromium("cache.browser.chrome", "Chrome", "%LOCALAPPDATA%\\Google\\Chrome"),
    _chromium("cache.browser.edge", "Edge", "%LOCALAPPDATA%\\Microsoft\\Edge"),
    _chromium(
        "cache.browser.yandex", "Яндекс.Браузера", "%LOCALAPPDATA%\\Yandex\\YandexBrowser"
    ),
    _chromium(
        "cache.browser.brave",
        "Brave",
        "%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser",
    ),
    # Opera keeps one profile per installed edition rather than a "User
    # Data" tree, so its cache sits directly under the edition directory.
    _chromium(
        "cache.browser.opera", "Opera", "%LOCALAPPDATA%\\Opera Software\\Opera *",
        profiles="",
    ),
    CleanupCategory(
        id="cache.browser.firefox",
        name="Кэш Firefox",
        description="Кэш страниц Firefox. Не куки, не пароли, не история.",
        rationale=(
            "Кэшированные ресурсы страниц, которые Firefox скачивает заново "
            "при следующем посещении. Профиль, пароли и история лежат в "
            "другом месте и не затрагиваются."
        ),
        # Firefox names profile directories randomly, e.g.
        # "8f3k2p1q.default-release", so the wildcard is the only way in.
        roots=("%LOCALAPPDATA%\\Mozilla\\Firefox\\Profiles\\*\\cache2",),
        min_age_hours=0.0,
        risk=CleanupRisk.LOW,
    ),
    # -- game launchers ----------------------------------------------------
    # Each entry names a launcher's own log, cache or installer-payload
    # directory. None of them names a game: the protected-path guard still
    # refuses "steamapps", "Epic Games" and the rest, and the two vendors
    # whose own folder name is itself on that list waive it for their own
    # subdirectories only.
    #
    # Unverified on the target platform: none of these launchers is
    # installed on the development machine, so the paths come from vendor
    # layout rather than observation. A launcher that is absent, or whose
    # layout differs, reports "на этом ПК нет" and clears nothing — it
    # cannot delete the wrong thing, only find nothing.
    CleanupCategory(
        id="cache.launcher.epic",
        name="Кэш Epic Games Launcher",
        description="Логи и веб-кэш лаунчера Epic Games. Не игры.",
        rationale=(
            "Лаунчер пишет сюда журналы и кэш встроенного браузера "
            "магазина. Создаётся заново при следующем запуске; сами игры "
            "лежат в другом месте и защищены списком путей."
        ),
        roots=(
            "%LOCALAPPDATA%\\EpicGamesLauncher\\Saved\\Logs",
            "%LOCALAPPDATA%\\EpicGamesLauncher\\Saved\\webcache*",
        ),
        min_age_hours=0.0,
        risk=CleanupRisk.LOW,
    ),
    CleanupCategory(
        id="cache.launcher.battlenet",
        name="Кэш Battle.net",
        description=(
            "Кэш, журналы и скачанные установщики Battle.net. Не игры."
        ),
        rationale=(
            "Battle.net держит кэш агента и загруженные пакеты установки "
            "после того, как игра уже установлена; на клубном ПК это "
            "несколько гигабайт, которые больше не нужны."
        ),
        roots=(
            "%LOCALAPPDATA%\\Battle.net\\Cache",
            "%LOCALAPPDATA%\\Battle.net\\Logs",
            "%PROGRAMDATA%\\Battle.net\\Setup",
        ),
        min_age_hours=24.0,
        risk=CleanupRisk.LOW,
        # "battle.net" is a protected vendor name so that no category can
        # reach a game through it. These roots are the launcher's own cache.
        allowed_path_fragments=("battle.net",),
    ),
    CleanupCategory(
        id="cache.launcher.riot",
        name="Журналы Riot Client",
        description="Журналы клиента Riot. Не игры и не настройки.",
        rationale=(
            "Текстовые журналы работы клиента. Нужны только при разборе "
            "конкретного сбоя и растут без ограничения."
        ),
        roots=("%LOCALAPPDATA%\\Riot Games\\Riot Client\\Logs",),
        min_age_hours=24.0,
        risk=CleanupRisk.LOW,
        # Same reasoning as Battle.net: the vendor folder is protected, this
        # one subdirectory under it is a log folder.
        allowed_path_fragments=("riot games",),
    ),
    CleanupCategory(
        id="cache.launcher.ea",
        name="Кэш EA App и Origin",
        description="Кэш и журналы EA Desktop и Origin. Не игры.",
        rationale=(
            "Кэш магазина и журналы работы клиента. Создаются заново; "
            "библиотека игр хранится отдельно."
        ),
        roots=(
            "%LOCALAPPDATA%\\Electronic Arts\\EA Desktop\\Logs",
            "%LOCALAPPDATA%\\Electronic Arts\\EA Desktop\\cache",
            "%PROGRAMDATA%\\Electronic Arts\\EA Desktop\\Logs",
            "%LOCALAPPDATA%\\Origin\\Logs",
            "%PROGRAMDATA%\\Origin\\Logs",
        ),
        min_age_hours=24.0,
        risk=CleanupRisk.LOW,
        # Every root is the launcher's own log or cache folder under the
        # publisher's name; the games are not beneath any of them.
        allowed_path_fragments=("electronic arts",),
    ),
    CleanupCategory(
        id="cache.launcher.ubisoft",
        name="Кэш Ubisoft Connect",
        description="Кэш и журналы Ubisoft Connect. Не игры и не сохранения.",
        rationale=(
            "Кэш магазина и журналы лаунчера. Облачные сохранения хранятся "
            "у Ubisoft, а установленные игры — в другом каталоге."
        ),
        roots=(
            "%LOCALAPPDATA%\\Ubisoft Game Launcher\\logs",
            "%LOCALAPPDATA%\\Ubisoft Game Launcher\\cache",
        ),
        min_age_hours=24.0,
        risk=CleanupRisk.LOW,
        # The launcher keeps its own folder under its publisher's name; the
        # installed games live elsewhere, in the install directory.
        allowed_path_fragments=("ubisoft",),
    ),
    # -- the user's own files ----------------------------------------------
    CleanupCategory(
        id="files.downloads",
        name="Папка «Загрузки»",
        description=(
            "Всё, что скачано в папку «Загрузки» и лежит там дольше трёх суток."
        ),
        rationale=(
            "На клубном ПК «Загрузки» накапливают установщики, архивы и "
            "случайные файлы прошлых посетителей. В отличие от остальных "
            "категорий это файлы пользователя, а не данные, которые система "
            "создаст заново: восстановить их можно только повторной "
            "загрузкой. Поэтому категория выключена по умолчанию и её "
            "включают осознанно."
        ),
        roots=("%USERPROFILE%\\Downloads",),
        min_age_hours=72.0,
        risk=CleanupRisk.MEDIUM,
        # Someone's folder structure in Downloads is theirs; emptying a
        # folder is not a reason to also delete the folder.
        remove_empty_dirs=False,
        enabled_by_default=False,
        clears_user_files=True,
    ),
)


CATEGORIES_BY_ID: dict[str, CleanupCategory] = {c.id: c for c in CATEGORIES}


def default_categories() -> tuple[CleanupCategory, ...]:
    """Categories enabled unless the operator says otherwise.

    Two are off: the Downloads folder holds the user's own files, and the
    Windows servicing logs are occasionally needed to diagnose a failed
    update. Both are one tick away in the cleanup dialog.
    """
    return tuple(c for c in CATEGORIES if c.enabled_by_default)


__all__ = ["CATEGORIES", "CATEGORIES_BY_ID", "default_categories"]
