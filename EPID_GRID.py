import gc
import math
import os
import time
import traceback

from qgis.PyQt.QtCore import QVariant, Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QFileDialog,
    QProgressDialog,
)

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from qgis.utils import iface
TARGET_CRS = "EPSG:3576"
REFERENCE_RADIUS_M = 4_135_000.0
BASE_AREA_M2 = 1_000_000.0
BLOCK_SIZE = 16
BATCH_SIZE = 5000
OUTPUT_LAYER_NAME = "grid_1km"
REPAIR_INVALID_GEOMETRIES = True
UI_INTERVAL_SECONDS = 0.3
MERGE_LATITUDE_2 = 60.0
MERGE_LATITUDE_4 = 70.0
def get_grid_parameters():
    approximate_count = (
        2.0 * math.pi * REFERENCE_RADIUS_M
        / math.sqrt(BASE_AREA_M2)
    )

    angular_blocks = max(
        1,
        int(round(approximate_count / BLOCK_SIZE)),
    )

    sector_count = angular_blocks * BLOCK_SIZE

    delta_theta = 2.0 * math.pi / sector_count

    delta_u = (
        2.0 * BASE_AREA_M2
        / math.sin(delta_theta)
    )

    return (
        sector_count,
        angular_blocks,
        delta_theta,
        delta_u,
    )


def get_quadtree_code(radial_local, angular_local):
    code = []

    for bit in (3, 2, 1, 0):
        radial_bit = (radial_local >> bit) & 1
        angular_bit = (angular_local >> bit) & 1

        if radial_bit:
            digit = 1 if angular_bit == 0 else 2
        else:
            digit = 3 if angular_bit == 0 else 4

        code.append(str(digit))

    return "".join(code)


def get_sector_envelope(
    r_inner,
    r_outer,
    theta_start,
    theta_end,
):

    angles = [theta_start, theta_end]

    quarter_turn = math.pi / 2.0

    first = math.ceil(theta_start / quarter_turn)
    last = math.floor(theta_end / quarter_turn)

    for k in range(first, last + 1):
        angles.append(k * quarter_turn)

    xs = []
    ys = []

    for angle in angles:
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        for radius in (r_inner, r_outer):
            xs.append(radius * cos_a)
            ys.append(radius * sin_a)

    if r_inner == 0.0:
        xs.append(0.0)
        ys.append(0.0)

    epsilon = 0.001

    return QgsRectangle(
        min(xs) - epsilon,
        min(ys) - epsilon,
        max(xs) + epsilon,
        max(ys) + epsilon,
    )


def create_cell_geometry(
    i,
    j,
    sector_count,
    delta_theta,
    delta_u,
):

    theta1 = i * delta_theta

    theta2 = (
        0.0
        if i + 1 == sector_count
        else (i + 1) * delta_theta
    )

    r1 = math.sqrt(j * delta_u)
    r2 = math.sqrt((j + 1) * delta_u)

    cos_1 = math.cos(theta1)
    sin_1 = math.sin(theta1)

    cos_2 = math.cos(theta2)
    sin_2 = math.sin(theta2)

    if j == 0:
        ring = [
            QgsPointXY(0.0, 0.0),
            QgsPointXY(r2 * cos_2, r2 * sin_2),
            QgsPointXY(r2 * cos_1, r2 * sin_1),
            QgsPointXY(0.0, 0.0),
        ]
    else:
        p1 = QgsPointXY(r1 * cos_1, r1 * sin_1)
        p2 = QgsPointXY(r1 * cos_2, r1 * sin_2)
        p3 = QgsPointXY(r2 * cos_2, r2 * sin_2)
        p4 = QgsPointXY(r2 * cos_1, r2 * sin_1)

        ring = [p1, p2, p3, p4, p1]

    return QgsGeometry.fromPolygonXY([ring])


def create_output_fields():
    fields = QgsFields()

    definitions = [
        ("ID_L1", QVariant.String),
        ("ID_L2", QVariant.String),
        ("ID_L3", QVariant.String),
        ("ID_L4", QVariant.String),
        ("ID_L5", QVariant.String),
        ("Area_km2", QVariant.Double),
        ("MERGE_N", QVariant.Int),
        ("MERGE_ID", QVariant.String),
    ]

    for name, field_type in definitions:
        fields.append(QgsField(name, field_type))

    return fields


def generate_grid():
    layer = iface.activeLayer()

    if (
        layer is None
        or not isinstance(layer, QgsVectorLayer)
        or not layer.isValid()
    ):
        iface.messageBar().pushCritical(
            "Ошибка",
            "Выберите корректный векторный слой границ.",
        )
        return

    if (
        QgsWkbTypes.geometryType(layer.wkbType())
        != QgsWkbTypes.PolygonGeometry
    ):
        iface.messageBar().pushCritical(
            "Ошибка",
            "Активный слой должен быть полигональным.",
        )
        return

    if not layer.crs().isValid():
        iface.messageBar().pushCritical(
            "Ошибка",
            "У исходного слоя не определена система координат.",
        )
        return

    output_path, _ = QFileDialog.getSaveFileName(
        iface.mainWindow(),
        "Сохранить сетку в новый GeoPackage",
        "",
        "GeoPackage (*.gpkg)",
    )

    if not output_path:
        return

    if not output_path.lower().endswith(".gpkg"):
        output_path += ".gpkg"

    if os.path.exists(output_path):
        iface.messageBar().pushCritical(
            "Ошибка",
            "Файл уже существует. Выберите новое имя.",
        )
        return

    target_crs = QgsCoordinateReferenceSystem(TARGET_CRS)
    geographic_crs = QgsCoordinateReferenceSystem("EPSG:4326")

    transform_context = (
        QgsProject.instance().transformContext()
    )

    source_transform = QgsCoordinateTransform(
        layer.crs(),
        target_crs,
        transform_context,
    )

    latitude_transform = QgsCoordinateTransform(
        target_crs,
        geographic_crs,
        transform_context,
    )

    (
        sector_count,
        angular_block_count,
        delta_theta,
        delta_u,
    ) = get_grid_parameters()

    fields = create_output_fields()

    selected_only = layer.selectedFeatureCount() > 0

    source_count = (
        layer.selectedFeatureCount()
        if selected_only
        else layer.featureCount()
    )

    progress = QProgressDialog(
        "Подготовка исходных границ…",
        "Отмена",
        0,
        0,
        iface.mainWindow(),
    )

    progress.setWindowTitle("Создание сетки")
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.show()

    QApplication.processEvents()

    started = time.monotonic()
    last_ui = started

    writer = None
    status = "preparing"
    error_text = None

    geometries = {}
    spatial_index = QgsSpatialIndex()
    extent = None

    batch = []

    source_read = 0
    repaired_count = 0

    created = 0
    checked_cells = 0
    checked_blocks = 0
    accepted_blocks = 0

    merge_cache = {}

    def update_ui(text, force=False):
        nonlocal last_ui

        now = time.monotonic()

        if force or now - last_ui >= UI_INTERVAL_SECONDS:
            progress.setLabelText(text)
            QApplication.processEvents()
            last_ui = now

        return progress.wasCanceled()

    def write_batch():
        if not batch:
            return

        if not writer.addFeatures(
            batch,
            QgsFeatureSink.FastInsert,
        ):
            raise RuntimeError(
                "Ошибка записи: " + writer.lastError()
            )

        batch.clear()

    def get_merge_count(j):
        if j in merge_cache:
            return merge_cache[j]

        radius_mid = math.sqrt(
            (j + 0.5) * delta_u
        )

        reference_point = QgsPointXY(
            0.0,
            -radius_mid,
        )

        geographic_point = latitude_transform.transform(
            reference_point
        )

        latitude = geographic_point.y()

        if not math.isfinite(latitude):
            raise ValueError(
                f"Не удалось определить широту ряда j={j}."
            )

        if latitude >= MERGE_LATITUDE_4:
            merge_n = 4
        elif latitude >= MERGE_LATITUDE_2:
            merge_n = 2
        else:
            merge_n = 1

        merge_cache[j] = merge_n

        return merge_n

    try:
        iterator = (
            layer.getSelectedFeatures()
            if selected_only
            else layer.getFeatures()
        )

        for source_feature in iterator:
            source_read += 1

            geometry = QgsGeometry(
                source_feature.geometry()
            )

            if geometry.isNull() or geometry.isEmpty():
                continue

            if layer.crs() != target_crs:
                geometry.transform(source_transform)

            if not geometry.isGeosValid():
                if not REPAIR_INVALID_GEOMETRIES:
                    raise ValueError(
                        "Некорректная исходная геометрия: "
                        f"{source_feature.id()}."
                    )

                geometry = geometry.makeValid()
                repaired_count += 1

            if geometry.isNull() or geometry.isEmpty():
                raise ValueError(
                    "Не удалось восстановить геометрию: "
                    f"{source_feature.id()}."
                )

            if (
                QgsWkbTypes.geometryType(geometry.wkbType())
                != QgsWkbTypes.PolygonGeometry
            ):
                raise ValueError(
                    f"Объект {source_feature.id()} после исправления "
                    "не является полигональным. "
                    "Исправьте исходный слой отдельно."
                )

            if not geometry.isGeosValid():
                raise ValueError(
                    "Геометрия остается некорректной: "
                    f"{source_feature.id()}."
                )

            box = geometry.boundingBox()

            bounds = (
                box.xMinimum(),
                box.xMaximum(),
                box.yMinimum(),
                box.yMaximum(),
            )

            if not all(math.isfinite(v) for v in bounds):
                raise ValueError(
                    "Нечисловые координаты объекта: "
                    f"{source_feature.id()}."
                )

            internal_id = len(geometries)
            geometries[internal_id] = geometry

            index_feature = QgsFeature()
            index_feature.setId(internal_id)
            index_feature.setGeometry(geometry)

            if not spatial_index.addFeature(index_feature):
                raise RuntimeError(
                    "Не удалось добавить геометрию "
                    "в пространственный индекс."
                )

            if extent is None:
                extent = QgsRectangle(box)
            else:
                extent.combineExtentWith(box)

            if update_ui(
                f"Подготовка границ: {source_read:,} / "
                f"{source_count:,}\n"
                f"Принято геометрий: {len(geometries):,}\n"
                f"Исправлено: {repaired_count:,}"
            ):
                status = "cancelled_before_generation"
                break

        if status != "cancelled_before_generation":
            if not geometries or extent is None:
                raise ValueError(
                    "Нет пригодных геометрий для расчета."
                )

            corners = [
                (extent.xMinimum(), extent.yMinimum()),
                (extent.xMinimum(), extent.yMaximum()),
                (extent.xMaximum(), extent.yMinimum()),
                (extent.xMaximum(), extent.yMaximum()),
            ]

            max_u = max(
                x*x + y*y for x, y in corners
            )

            radial_cell_count = (
                math.ceil(max_u / delta_u) + 1
            )

            radial_block_count = math.ceil(
                radial_cell_count / BLOCK_SIZE
            )

            total_blocks = (
                angular_block_count * radial_block_count
            )

            print(
                "\n=== Создание сетки ===\n"
                f"Используется выделение: {selected_only}\n"
                f"Исходных геометрий: {len(geometries):,}\n"
                f"Блоков-кандидатов: {total_blocks:,}\n"
                f"Выход: {output_path}\n"
            )

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.fileEncoding = "UTF-8"
            options.layerName = OUTPUT_LAYER_NAME
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile
            )
            options.layerOptions = ["SPATIAL_INDEX=YES"]

            writer = QgsVectorFileWriter.create(
                output_path,
                fields,
                QgsWkbTypes.Polygon,
                target_crs,
                transform_context,
                options,
            )

            if writer is None:
                raise RuntimeError(
                    "Не удалось создать GeoPackage."
                )

            if (
                writer.hasError()
                != QgsVectorFileWriter.NoError
            ):
                raise RuntimeError(
                    writer.errorMessage()
                )

            progress.setRange(0, 1000)
            progress.setValue(0)

            status = "running"
            cancelled = False

            for angular_block in range(angular_block_count):
                i_start = angular_block * BLOCK_SIZE
                i_end = i_start + BLOCK_SIZE

                theta_start = i_start * delta_theta
                theta_end = i_end * delta_theta

                for radial_block in range(radial_block_count):
                    checked_blocks += 1

                    j_start = radial_block * BLOCK_SIZE

                    j_end = min(
                        j_start + BLOCK_SIZE,
                        radial_cell_count,
                    )

                    r_inner = math.sqrt(
                        j_start * delta_u
                    )

                    r_outer = math.sqrt(
                        j_end * delta_u
                    )

                    block_box = get_sector_envelope(
                        r_inner,
                        r_outer,
                        theta_start,
                        theta_end,
                    )

                    candidate_ids = spatial_index.intersects(
                        block_box
                    )

                    block_hits = []

                    if candidate_ids:
                        rectangle_geometry = (
                            QgsGeometry.fromRect(block_box)
                        )

                        block_hits = [
                            fid
                            for fid in candidate_ids
                            if geometries[fid].intersects(
                                rectangle_geometry
                            )
                        ]

                    if block_hits:
                        accepted_blocks += 1

                        for i in range(i_start, i_end):
                            for j in range(j_start, j_end):
                                checked_cells += 1

                                geometry = create_cell_geometry(
                                    i,
                                    j,
                                    sector_count,
                                    delta_theta,
                                    delta_u,
                                )

                                if not any(
                                    geometry.intersects(
                                        geometries[fid]
                                    )
                                    for fid in block_hits
                                ):
                                    continue

                                local_i = i % BLOCK_SIZE
                                local_j = j % BLOCK_SIZE

                                path = get_quadtree_code(
                                    local_j,
                                    local_i,
                                )

                                root = (
                                    f"R{j // BLOCK_SIZE}"
                                    f"T{i // BLOCK_SIZE}"
                                )

                                merge_n = get_merge_count(j)

                                merge_id = (
                                    f"M{merge_n}"
                                    f"R{j}"
                                    f"T{i // merge_n}"
                                )

                                feature = QgsFeature(fields)
                                feature.setGeometry(geometry)

                                feature.setAttributes([
                                    f"{root}-{path}",
                                    f"{root}-{path[:3]}",
                                    f"{root}-{path[:2]}",
                                    f"{root}-{path[:1]}",
                                    root,
                                    geometry.area() / 1_000_000.0,
                                    merge_n,
                                    merge_id,
                                ])

                                batch.append(feature)
                                created += 1

                                if len(batch) >= BATCH_SIZE:
                                    write_batch()

                            elapsed = (
                                time.monotonic() - started
                            )

                            if update_ui(
                                f"Блоков: {checked_blocks:,} / "
                                f"{total_blocks:,}\n"
                                f"Проверено ячеек: "
                                f"{checked_cells:,}\n"
                                f"Создано: {created:,}\n"
                                f"Прошло: {elapsed / 60.0:.1f} мин"
                            ):
                                cancelled = True
                                break

                        if cancelled:
                            break

                    progress.setValue(
                        int(
                            1000 * checked_blocks
                            / total_blocks
                        )
                    )

                    elapsed = time.monotonic() - started

                    if update_ui(
                        f"Блоков: {checked_blocks:,} / "
                        f"{total_blocks:,}\n"
                        f"Блоков с пересечением: "
                        f"{accepted_blocks:,}\n"
                        f"Проверено ячеек: {checked_cells:,}\n"
                        f"Создано: {created:,}\n"
                        f"Прошло: {elapsed / 60.0:.1f} мин"
                    ):
                        cancelled = True
                        break

                if cancelled:
                    break

            write_batch()

            if not writer.flushBuffer():
                raise RuntimeError(
                    "Ошибка завершения записи: "
                    + writer.lastError()
                )

            status = (
                "cancelled" if cancelled else "complete"
            )

    except Exception as exc:
        status = "failed"
        error_text = str(exc)
        traceback.print_exc()

    finally:
        if writer is not None:
            writer = None
            gc.collect()

        progress.close()

    if status == "cancelled_before_generation":
        iface.messageBar().pushWarning(
            "Отмена",
            "Расчет отменен до создания выходного слоя.",
        )
        return

    if status == "failed":
        iface.messageBar().pushCritical(
            "Ошибка",
            f"{error_text}. "
            "Если файл создан, он может быть неполным.",
        )
        return

    result = QgsVectorLayer(
        f"{output_path}|layername={OUTPUT_LAYER_NAME}",
        (
            "Сетка_неполная"
            if status == "cancelled"
            else "Сетка"
        ),
        "ogr",
    )

    if not result.isValid():
        iface.messageBar().pushCritical(
            "Ошибка",
            "Выходной слой не удалось открыть.",
        )
        return

    # Параметры отрисовки не изменяются.
    QgsProject.instance().addMapLayer(result)

    actual_count = result.featureCount()
    elapsed = time.monotonic() - started

    print(
        f"\nСтатус: {status}\n"
        f"Ячеек в GeoPackage: {actual_count:,}\n"
        f"Время: {elapsed / 60.0:.1f} мин\n"
        f"Файл: {output_path}\n"
    )

    if status == "cancelled":
        iface.messageBar().pushWarning(
            "Расчет отменен",
            f"Сохранена неполная сетка: "
            f"{actual_count:,} ячеек.",
        )
    else:
        iface.messageBar().pushSuccess(
            "Готово",
            f"Создано {actual_count:,} отдельных ячеек.",
        )
generate_grid()
