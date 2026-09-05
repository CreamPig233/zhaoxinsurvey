"""Export the current final questionnaire submissions to CSV."""

import argparse
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "data" / "survey.sqlite3"
DEFAULT_OUTPUT = BASE_DIR / "last_survey.csv"
LEGACY_MEDICAL_COLLEGES = {
    "医学与生物信息工程学院（原中荷生物医学与信息工程学院）",
    "医学与生物信息工程学院（中荷生物医学与信息工程学院）",
}
MEDICAL_COLLEGE = "医学与生物信息工程学院"
HEADERS = [
    "QQ号",
    "姓名",
    "学号",
    "学院",
    "QQ群名片",
    "校区",
    "专业",
    "性别",
    "部门志愿1",
    "部门志愿2",
    "部门志愿3",
    "部门志愿4",
    "部门志愿5",
    "是否服从调剂",
    "个人简介",
    "其它特长",
    "填写时间",
]


def parse_departments(value: str, student_id: str) -> list[str]:
    """Return departments in their submitted order."""
    try:
        departments = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"学号 {student_id} 的部门志愿数据不是有效 JSON") from error

    if not isinstance(departments, list) or not all(isinstance(item, str) for item in departments):
        raise ValueError(f"学号 {student_id} 的部门志愿数据格式无效")
    return departments


def submission_rows(connection: sqlite3.Connection):
    """Read submissions while tolerating databases without a submitted_at column."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(submissions)")}
    if "submitted_at" in columns:
        submitted_at_expression = "submitted_at"
        order_by = "submitted_at, student_id"
    else:
        submitted_at_expression = "''"
        order_by = "student_id"

    major_expression = "major" if "major" in columns else "''"
    college_expression = "college" if "college" in columns else "''"
    group_card_expression = "group_card" if "group_card" in columns else "''"
    campus_expression = "campus" if "campus" in columns else "'未知'"
    return connection.execute(
        f"""
        SELECT qq, name, {college_expression} AS college, {group_card_expression} AS group_card,
               {campus_expression} AS campus, {major_expression} AS major,
               gender, departments_json, transfer, strengths, other_talents, student_id,
               {submitted_at_expression} AS submitted_at
        FROM submissions
        ORDER BY {order_by}
        """
    )


def export_submissions(database: Path, output: Path) -> int:
    """Export all current rows from submissions and return the row count."""
    if not database.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {database}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, output.open(
        "w", encoding="utf-8-sig", newline=""
    ) as file:
        connection.row_factory = sqlite3.Row
        rows = submission_rows(connection)

        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(HEADERS)
        count = 0
        for row in rows:
            departments = parse_departments(row["departments_json"], row["student_id"])
            department_columns = (departments + [""] * 5)[:5]
            writer.writerow(
                [
                    row["qq"],
                    row["name"],
                    row["student_id"],
                    MEDICAL_COLLEGE if row["college"] in LEGACY_MEDICAL_COLLEGES else row["college"],
                    row["group_card"],
                    row["campus"],
                    row["major"],
                    row["gender"],
                    *department_columns,
                    row["transfer"],
                    row["strengths"],
                    row["other_talents"],
                    row["submitted_at"] or "",
                ]
            )
            count += 1

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="导出最后一次问卷数据 CSV")
    parser.add_argument(
        "-d",
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite 数据库路径（默认：{DEFAULT_DATABASE}）",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"CSV 输出路径（默认：{DEFAULT_OUTPUT}）",
    )
    args = parser.parse_args()

    count = export_submissions(args.database, args.output)
    print(f"已导出 {count} 条问卷数据到 {args.output}")


if __name__ == "__main__":
    main()
