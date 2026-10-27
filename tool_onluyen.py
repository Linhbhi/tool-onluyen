#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tool-onluyen: Terminal UI tự động giải và nộp bài tập onluyen.vn
Hỗ trợ solver qua:
  1. Google GenAI Python SDK (google-genai client.models.generate_content)
  2. Jev Zero-shot Classifier (https://classifier.dev/)
Tự động lưu và đọc cấu hình từ file .env.
"""

import os
import sys
import re
import json
import time
import base64
import urllib.parse
from typing import Dict, List, Any, Optional, Tuple

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.prompt import Prompt, Confirm
from rich import print as rprint

console = Console()

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Mobile Safari/537.36"
)

ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


# ==============================================================================
# QUẢN LÝ CẤU HÌNH .ENV
# ==============================================================================
def load_env_config() -> Dict[str, str]:
    """Đọc cấu hình từ file .env."""
    config = {}
    if os.path.exists(ENV_FILE_PATH):
        try:
            with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        config[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
    return config


def save_env_config(updates: Dict[str, str]):
    """Ghi hoặc cập nhật cấu hình vào file .env mà không mất các dòng comment."""
    current = {}
    lines = []
    if os.path.exists(ENV_FILE_PATH):
        try:
            with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []

    # Parse current keys
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            current[k.strip()] = v.strip().strip("'\"")

    # Update
    current.update(updates)

    # Write back clean
    with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
        f.write("# Cấu hình tài khoản Onluyen & Gemini Key (tự động cập nhật)\n")
        for k, v in current.items():
            f.write(f"{k}={v}\n")


def clean_html(raw_html: str) -> str:
    """Loại bỏ HTML thừa, giữ lại link ảnh Markdown và định dạng dòng."""
    if not raw_html:
        return ""
    raw_html = re.sub(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', r' ![Hình ảnh](\1) ', raw_html)
    raw_html = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r'</p>', '\n', raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;|&apos;", "'", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


# ==============================================================================
# ONLUYEN API CLIENT (SCHOOL ONLINE ENDPOINTS)
# ==============================================================================
class OnluyenClient:
    LOGIN_URL = "https://oauth.onluyen.vn/api/account/login"
    BASE_API = "https://api-elb.onluyen.vn/api"

    def __init__(self, username: str = "", password: str = ""):
        self.username = username
        self.password = password
        self.token = ""
        self.user_id = ""
        self.display_name = ""
        self.school_id = "648"
        self.school_year = "2026"
        self.code_app = "SCHOOL"
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json, text/plain, */*",
            "origin": "https://app.onluyen.vn",
            "referer": "https://app.onluyen.vn/",
            "user-agent": DEFAULT_USER_AGENT
        })

    def login(self, username: Optional[str] = None, password: Optional[str] = None) -> bool:
        u = username or self.username
        p = password or self.password
        if not u or not p:
            return False

        payload = {
            "userName": u,
            "password": p,
            "codeApp": self.code_app
        }
        try:
            res = self.session.post(self.LOGIN_URL, json=payload, timeout=15)
            if res.status_code == 200:
                data = res.json()
                self.token = data.get("access_token", "")
                self.username = u
                self.password = p

                if "." in self.token:
                    try:
                        payload_part = self.token.split(".")[1]
                        padded = payload_part + "=" * (-len(payload_part) % 4)
                        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
                        self.user_id = decoded.get("sub", "")
                        self.display_name = decoded.get("name", "") or decoded.get("displayName", "")
                    except Exception:
                        pass

                self.session.headers.update({
                    "authorization": f"Bearer {self.token}",
                    "schoolyear": self.school_year,
                    "schoolid": self.school_id,
                    "userid": self.user_id
                })
                return True
        except Exception as e:
            console.print(f"[red]Lỗi đăng nhập: {e}[/red]")
        return False

    def get_assignments(self, filter_status: str = "all", page_size: int = 50) -> List[Dict[str, Any]]:
        """Lấy danh sách bài tập được giao từ school-online endpoint."""
        url = f"{self.BASE_API}/school-online/assignment/assign-student-suggest?pageIndex=1&pageSize={page_size}"
        res = self.session.get(url, timeout=15)
        if res.status_code != 200:
            return []

        items = res.json().get("data", [])
        result = []
        for item in items:
            st = item.get("status", 0)
            if filter_status == "todo" and st not in (0, 1):
                continue
            if filter_status == "done" and st not in (2, 3):
                continue

            status_desc = "Đã nộp" if st in (2, 3) else "Đang làm" if st == 1 else "Chưa làm"
            result.append({
                "assignId": item.get("assignId"),
                "name": item.get("name"),
                "className": item.get("className", ""),
                "status": st,
                "statusText": status_desc,
                "totalScore": item.get("totalScore"),
                "score": item.get("score"),
                "timeStart": item.get("timeStart"),
                "timeEnd": item.get("timeEnd")
            })
        return result

    def get_test_data(self, assign_id: str) -> Optional[Dict[str, Any]]:
        """Lấy đề thi và danh sách câu hỏi."""
        # 1. Kích hoạt start nếu bài mới
        url_start = f"{self.BASE_API}/school-online/assignment/start/{assign_id}"
        self.session.post(url_start, json={}, timeout=15)

        # 2. Tải doing data
        url_doing = f"{self.BASE_API}/school-online/assignment/doing/{assign_id}"
        res = self.session.get(url_doing, timeout=20)
        if res.status_code == 200:
            return res.json().get("data")

        # Fallback detail
        url_detail = f"{self.BASE_API}/school-online/assignment/detail/{assign_id}"
        res_detail = self.session.get(url_detail, timeout=20)
        if res_detail.status_code == 200:
            return res_detail.json().get("data")

        return None

    def upload_to_s3(self, put_url: str, s3_answers: List[Dict[str, Any]]) -> bool:
        """Upload trực tiếp đáp án lên AWS S3."""
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://app.onluyen.vn",
            "Referer": "https://app.onluyen.vn/"
        }
        res = requests.put(put_url, data=json.dumps(s3_answers, ensure_ascii=False), headers=headers, timeout=20)
        return res.status_code == 200

    def submit(self, assign_id: str) -> Dict[str, Any]:
        """Gửi lệnh nộp bài."""
        url = f"{self.BASE_API}/school-online/assignment/submit/{assign_id}"
        res = self.session.post(url, json={}, timeout=20)
        if res.status_code == 200:
            return res.json()
        return {"success": False, "message": f"HTTP {res.status_code}: {res.text}"}

    def get_result_detail(self, assign_id: str) -> Optional[Dict[str, Any]]:
        """Lấy bảng điểm chi tiết và lời giải."""
        url = f"{self.BASE_API}/school-online/assignment/detail/{assign_id}"
        res = self.session.get(url, timeout=15)
        if res.status_code == 200:
            return res.json().get("data")
        return None


# ==============================================================================
# AI SOLVERS: GEMINI SDK & JEV CLASSIFIER
# ==============================================================================
class BaseSolver:
    def solve_choice(self, question: str, options: List[str]) -> str:
        raise NotImplementedError

    def solve_true_false(self, question: str, statements: List[str]) -> List[str]:
        raise NotImplementedError

    def solve_text(self, question: str) -> str:
        raise NotImplementedError


class JevClassifierSolver(BaseSolver):
    """Zero-shot text classifier via classifier.dev."""
    BASE_URL = "https://classifier.dev"

    def _query(self, labels: List[str], text: str) -> str:
        safe_labels = ",".join(labels)
        safe_text = urllib.parse.quote(text[:800])
        url = f"{self.BASE_URL}/{safe_labels}/{safe_text}"
        try:
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                return res.text.strip()
        except Exception:
            pass
        return labels[0]

    def solve_choice(self, question: str, options: List[str]) -> str:
        opt_labels = ["A", "B", "C", "D"][:len(options)]
        query = f"Câu hỏi: {question}\n" + "\n".join(options)
        pred = self._query(opt_labels, query)
        for label in opt_labels:
            if label.lower() in pred.lower():
                return label
        return "A"

    def solve_true_false(self, question: str, statements: List[str]) -> List[str]:
        results = []
        for stmt in statements:
            query = f"Trong ngữ cảnh câu hỏi: '{question}'. Nhận định sau đây là đúng hay sai: '{stmt}'"
            pred = self._query(["Dung", "Sai"], query)
            if "dung" in pred.lower():
                results.append("Đ")
            else:
                results.append("S")
        return results

    def solve_text(self, question: str) -> str:
        return "1"


class GeminiSolver(BaseSolver):
    """
    Google GenAI SDK (google-genai client.models.generate_content)
    với REST API fallback.
    """
    def __init__(self, api_key: str, model: str = "gemini-3.8-flash"):
        self.api_key = api_key
        self.model = model
        self.genai_client = None

        try:
            from google import genai
            from google.genai import types
            self.genai_client = genai.Client(api_key=self.api_key)
            self.types = types
        except Exception:
            self.genai_client = None

    def _call(self, prompt: str) -> str:
        if self.genai_client:
            try:
                config = self.types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=256
                )
                response = self.genai_client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config
                )
                if response and response.text:
                    return response.text.strip()
            except Exception:
                fallback_chain = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
                for next_model in fallback_chain:
                    if next_model != self.model:
                        try:
                            self.model = next_model
                            response = self.genai_client.models.generate_content(
                                model=self.model,
                                contents=prompt,
                                config=config
                            )
                            if response and response.text:
                                return response.text.strip()
                        except Exception:
                            pass

        # Fallback REST API
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 256
            }
        }
        try:
            res = requests.post(endpoint, headers=headers, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
        except Exception as e:
            console.print(f"[yellow]Lỗi gọi Gemini API: {e}[/yellow]")

        return ""

    def solve_choice(self, question: str, options: List[str]) -> str:
        prompt = (
            f"Bạn là chuyên gia giải bài tập. Hãy trả lời câu hỏi trắc nghiệm sau.\n"
            f"Câu hỏi: {question}\n"
            f"Các lựa chọn:\n" + "\n".join(options) + "\n\n"
            f"Chỉ trả lời DUY NHẤT một chữ cái phương án đúng nhất (ví dụ: A, B, C hoặc D). Không giải thích thêm."
        )
        raw = self._call(prompt)
        match = re.search(r"\b([A-D])\b", raw.upper())
        if match:
            return match.group(1)
        return "A"

    def solve_true_false(self, question: str, statements: List[str]) -> List[str]:
        prompt = (
            f"Đánh giá tính Đúng hoặc Sai của từng ý sau dựa trên câu hỏi:\n"
            f"Đề bài: {question}\n"
            f"Các ý cần xét:\n" + "\n".join([f"{i+1}. {st}" for i, st in enumerate(statements)]) + "\n\n"
            f"Trả về kết quả dưới dạng danh sách chữ cái Đ hoặc S cách nhau bởi dấu phẩy.\n"
            f"Ví dụ: Đ, S, Đ, S. Không viết gì thêm."
        )
        raw = self._call(prompt)
        tokens = [t.strip().upper() for t in raw.replace(".", ",").split(",") if t.strip()]
        res = []
        for t in tokens:
            if "Đ" in t or "D" in t or "TRUE" in t:
                res.append("Đ")
            elif "S" in t or "FALSE" in t:
                res.append("S")
        while len(res) < len(statements):
            res.append("Đ")
        return res[:len(statements)]

    def solve_text(self, question: str) -> str:
        prompt = (
            f"Hãy giải bài tập và đưa ra đáp số cuối cùng cho câu hỏi sau:\n"
            f"Câu hỏi: {question}\n"
            f"Chỉ trả về DUY NHẤT giá trị đáp số (số hoặc từ khóa ngắn gọn). Không giải thích."
        )
        raw = self._call(prompt)
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        return lines[-1] if lines else "0"


# ==============================================================================
# HÀM PHỤ TRỢ PARSE CÂU HỎI
# ==============================================================================
def parse_raw_questions(raw_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    questions = []
    for idx, item in enumerate(raw_list):
        std = item.get("dataStandard", {})
        vi = std.get("languagesData", {}).get("vi", {})
        step_id = std.get("stepId")
        ta = std.get("typeAnswer", 0)
        q_text = clean_html(vi.get("content", ""))

        if ta == 1:
            q_type = "true_false"
        elif ta in (3, 5):
            q_type = "text"
        else:
            q_type = "choice"

        options = []
        for opt in vi.get("options", []):
            options.append({
                "idOption": opt.get("idOption"),
                "text": clean_html(opt.get("content", ""))
            })

        questions.append({
            "index": idx + 1,
            "stepId": step_id,
            "type": q_type,
            "typeAnswer": ta,
            "content": q_text,
            "options": options
        })
    return questions


# ==============================================================================
# TUI WORKFLOW CÁC CHỨC NĂNG
# ==============================================================================
def do_solve_workflow(client: OnluyenClient, solver: BaseSolver):
    """Quy trình chọn bài, giải bài tự động và nộp."""
    with console.status("[cyan]Đang tải danh sách bài tập chưa làm...[/cyan]"):
        assignments = client.get_assignments(filter_status="todo")

    if not assignments:
        console.print("[yellow]Không tìm thấy bài tập nào chưa làm trong tài khoản![/yellow]")
        return

    table = Table(title="Danh sách bài tập cần làm", border_style="blue")
    table.add_column("STT", style="cyan", width=5)
    table.add_column("Tên bài tập", style="white")
    table.add_column("Lớp", style="yellow", width=15)
    table.add_column("Trạng thái", style="green", width=12)

    for i, a in enumerate(assignments, 1):
        table.add_row(str(i), a["name"], a.get("className", ""), a["statusText"])

    console.print(table)
    idx_choice = Prompt.ask(f"[bold cyan]Chọn số thứ tự bài muốn làm (1 - {len(assignments)})[/bold cyan]", default="1")
    try:
        chosen_assignment = assignments[int(idx_choice) - 1]
    except Exception:
        console.print("[red]Lựa chọn không hợp lệ.[/red]")
        return

    assign_id = chosen_assignment["assignId"]
    console.print(f"\n[bold]Bắt đầu làm bài:[/bold] [yellow]{chosen_assignment['name']}[/yellow] (ID: {assign_id})")

    # Tải đề
    with console.status("[cyan]Đang tải câu hỏi...[/cyan]"):
        test_data = client.get_test_data(assign_id)

    if not test_data or not test_data.get("data"):
        console.print("[bold red]Không tải được dữ liệu câu hỏi![/bold red]")
        return

    questions_raw = test_data.get("data", [])
    put_url = test_data.get("preSignedUrlAnswer", "")
    start_time = test_data.get("startTime", int(time.time()))
    questions = parse_raw_questions(questions_raw)

    console.print(f"[green]✓ Đã tải {len(questions)} câu hỏi.[/green]\n")

    # Giải đề
    s3_answers = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        task = progress.add_task("[cyan]Đang giải câu hỏi...", total=len(questions))

        for q in questions:
            idx = q["index"]
            step_id = q["stepId"]
            q_type = q["type"]
            progress.update(task, description=f"[cyan]Đang giải câu {idx}/{len(questions)} ({q_type})...")

            if q_type == "choice":
                opts_text = [f"{chr(65+i)}. {opt['text']}" for i, opt in enumerate(q["options"])]
                ans_letter = solver.solve_choice(q["content"], opts_text)
                letter_idx = ord(ans_letter.upper()) - 65
                if 0 <= letter_idx < len(q["options"]):
                    chosen_opt_id = q["options"][letter_idx]["idOption"]
                else:
                    chosen_opt_id = q["options"][0]["idOption"]

                s3_answers.append({
                    "stepId": step_id,
                    "optionId": [chosen_opt_id],
                    "optionText": []
                })

            elif q_type == "true_false":
                stmts = [opt["text"] for opt in q["options"]]
                tf_list = solver.solve_true_false(q["content"], stmts)
                opt_texts = ["true" if v == "Đ" else "false" for v in tf_list]
                s3_answers.append({
                    "stepId": step_id,
                    "optionId": [],
                    "optionText": opt_texts
                })

            else:
                text_ans = solver.solve_text(q["content"])
                s3_answers.append({
                    "stepId": step_id,
                    "optionId": [],
                    "optionText": [text_ans]
                })

            progress.advance(task)

    console.print(f"\n[bold green]✓ Đã giải xong toàn bộ {len(s3_answers)} câu hỏi![/bold green]")

    # Lưu S3
    with console.status("[cyan]Đang lưu đáp án lên server Onluyen (AWS S3)...[/cyan]"):
        saved = client.upload_to_s3(put_url, s3_answers)

    if saved:
        console.print("[bold green]✓ Đã lưu đáp án thành công lên hệ thống![/bold green]")
    else:
        console.print("[bold red]Không thể lưu đáp án lên S3. Vui lòng kiểm tra lại mạng.[/bold red]")
        return

    # Tùy chọn nộp
    console.print("\n[bold yellow]Tùy chọn nộp bài:[/bold yellow]")
    console.print("  [1] Nộp ngay (tự động chờ đủ 3 phút tối thiểu theo quy định Onluyen)")
    console.print("  [2] Hẹn giờ làm bài (ví dụ 10 phút, 15 phút rồi mới nộp)")
    console.print("  [3] Chỉ lưu đáp án (tự nộp thủ công sau)")
    submit_mode = Prompt.ask("Lựa chọn", choices=["1", "2", "3"], default="1")

    if submit_mode == "3":
        console.print("[bold green]Đã lưu bài thành công. Bạn có thể mở web để nộp sau.[/bold green]")
        return

    target_wait_seconds = 180
    if submit_mode == "2":
        minutes = int(Prompt.ask("[cyan]Số phút muốn hệ thống làm bài trước khi nộp[/cyan]", default="10"))
        target_wait_seconds = minutes * 60

    now = int(time.time())
    elapsed = now - start_time
    remaining = max(0, target_wait_seconds - elapsed)

    if remaining > 0:
        console.print(f"\n[yellow]⏳ Đang đếm ngược thời gian làm bài ({remaining}s còn lại)...[/yellow]")
        with Progress(console=console) as progress:
            wait_task = progress.add_task("[cyan]Đang chờ...", total=remaining)
            while remaining > 0:
                time.sleep(1)
                remaining -= 1
                progress.advance(wait_task)

    with console.status("[cyan]Đang nộp bài...[/cyan]"):
        res = client.submit(assign_id)

    if res.get("success"):
        console.print("[bold green]🎉 NỘP BÀI THÀNH CÔNG![/bold green]")
        result_info = res.get("result", {})
        score = result_info.get("totalScore") or result_info.get("score")
        if score is not None:
            console.print(f"[bold cyan]Điểm số của bạn:[/bold cyan] [bold green]{score}[/bold green] / 10")
    else:
        console.print(f"[bold red]Nộp bài chưa thành công:[/bold red] {res.get('message')}")


def do_view_results(client: OnluyenClient):
    """Xem kết quả các bài đã nộp."""
    with console.status("[cyan]Đang tải danh sách bài đã nộp...[/cyan]"):
        assignments = client.get_assignments(filter_status="done")

    if not assignments:
        console.print("[yellow]Chưa có bài tập nào đã nộp![/yellow]")
        return

    table = Table(title="Danh sách bài đã nộp", border_style="green")
    table.add_column("STT", style="cyan", width=5)
    table.add_column("Tên bài tập", style="white")
    table.add_column("Điểm số", style="bold green", width=10)
    table.add_column("Trạng thái", style="magenta", width=12)

    for i, a in enumerate(assignments, 1):
        score_str = str(a.get("score") or a.get("totalScore") or "Đã nộp")
        table.add_row(str(i), a["name"], score_str, a["statusText"])

    console.print(table)



def do_config_menu(client: OnluyenClient) -> Dict[str, str]:
    """Menu cấu hình tổng thể (Full Configuration TUI)."""
    env_config = load_env_config()
    while True:
        cfg_table = Table(title="BẢNG CẤU HÌNH HỆ THỐNG (.env)", border_style="magenta")
        cfg_table.add_column("Tùy chọn", style="cyan", width=30)
        cfg_table.add_column("Giá trị hiện tại", style="yellow")
        cfg_table.add_column("Mô tả", style="dim")

        u = env_config.get("ONLUYEN_USERNAME", "")
        p = env_config.get("ONLUYEN_PASSWORD", "")
        masked_p = "*" * min(len(p), 8) if p else "(chưa cài)"
        gemini_k = env_config.get("GEMINI_API_KEY", "")
        masked_k = f"{gemini_k[:6]}...{gemini_k[-4:]}" if len(gemini_k) > 10 else (gemini_k if gemini_k else "(chưa cài)")
        model = env_config.get("GEMINI_MODEL", "gemini-3.8-flash")
        solver_mode = env_config.get("DEFAULT_SOLVER", "1")
        solver_name = "Google Gemini SDK" if solver_mode == "1" else "Jev Classifier (classifier.dev)"
        school_id = env_config.get("SCHOOL_ID", client.school_id)
        school_year = env_config.get("SCHOOL_YEAR", client.school_year)
        def_wait = env_config.get("DEFAULT_WAIT_MINUTES", "10")

        cfg_table.add_row("[1] Tài khoản Onluyen", u or "(chưa cài)", "SĐT hoặc Email đăng nhập")
        cfg_table.add_row("[2] Mật khẩu Onluyen", masked_p, "Mật khẩu bảo mật")
        cfg_table.add_row("[3] Bộ giải mặc định (Solver)", solver_name, "Gemini hoặc Jev Classifier")
        cfg_table.add_row("[4] Gemini API Key", masked_k, "Key từ Google AI Studio")
        cfg_table.add_row("[5] Gemini Model", model, "gemini-3.8-flash (Argon 4), 2.5, ...")
        cfg_table.add_row("[6] Thời gian làm bài hẹn giờ", f"{def_wait} phút", "Số phút làm bài trước khi nộp")
        cfg_table.add_row("[7] Mã trường (School ID)", school_id, "Mã trường học kết nối")
        cfg_table.add_row("[8] Năm học (School Year)", school_year, "Niên khóa hiện tại")
        cfg_table.add_row("[0] Lưu & Quay lại Menu chính", "", "Áp dụng toàn bộ cài đặt")

        console.print(cfg_table)
        sub_choice = Prompt.ask("[bold green]Chọn mục muốn chỉnh sửa (0-8)[/bold green]", choices=["1", "2", "3", "4", "5", "6", "7", "8", "0"], default="0")

        if sub_choice == "0":
            break
        elif sub_choice == "1":
            new_u = Prompt.ask("Nhập tài khoản mới", default=u)
            save_env_config({"ONLUYEN_USERNAME": new_u})
            env_config["ONLUYEN_USERNAME"] = new_u
        elif sub_choice == "2":
            new_p = Prompt.ask("Nhập mật khẩu mới", password=True)
            if new_p:
                save_env_config({"ONLUYEN_PASSWORD": new_p})
                env_config["ONLUYEN_PASSWORD"] = new_p
        elif sub_choice == "3":
            console.print("  [1] Google Gemini API (Độ chính xác cao)")
            console.print("  [2] Jev Classifier API (classifier.dev) (Miễn phí, không cần key)")
            new_s = Prompt.ask("Chọn Solver mặc định", choices=["1", "2"], default=solver_mode)
            save_env_config({"DEFAULT_SOLVER": new_s})
            env_config["DEFAULT_SOLVER"] = new_s
        elif sub_choice == "4":
            new_k = Prompt.ask("Nhập Gemini API Key mới", default=gemini_k)
            save_env_config({"GEMINI_API_KEY": new_k})
            env_config["GEMINI_API_KEY"] = new_k
        elif sub_choice == "5":
            console.print("  [1] gemini-3.8-flash (Argon 4 - Mới nhất & Khuyên dùng)")
            console.print("  [2] gemini-2.5-flash")
            console.print("  [3] gemini-2.0-flash")
            console.print("  [4] Tùy chỉnh tên model khác")
            m_pick = Prompt.ask("Chọn model", choices=["1", "2", "3", "4"], default="1")
            model_map = {"1": "gemini-3.8-flash", "2": "gemini-2.5-flash", "3": "gemini-2.0-flash"}
            chosen_m = model_map.get(m_pick) or Prompt.ask("Nhập tên model đầy đủ", default=model)
            save_env_config({"GEMINI_MODEL": chosen_m})
            env_config["GEMINI_MODEL"] = chosen_m
        elif sub_choice == "6":
            new_w = Prompt.ask("Số phút hẹn giờ mặc định khi nộp bài", default=def_wait)
            save_env_config({"DEFAULT_WAIT_MINUTES": new_w})
            env_config["DEFAULT_WAIT_MINUTES"] = new_w
        elif sub_choice == "7":
            new_sid = Prompt.ask("Mã trường (School ID)", default=school_id)
            save_env_config({"SCHOOL_ID": new_sid})
            client.school_id = new_sid
            env_config["SCHOOL_ID"] = new_sid
        elif sub_choice == "8":
            new_sy = Prompt.ask("Năm học (School Year)", default=school_year)
            save_env_config({"SCHOOL_YEAR": new_sy})
            client.school_year = new_sy
            env_config["SCHOOL_YEAR"] = new_sy

    console.print("[bold green]✓ Cấu hình đã được lưu vào file .env thành công![/bold green]")
    return env_config

def main():
    console.print(Panel.fit(
        "[bold cyan]TOOL ONLUYEN - TERMINAL UI[/bold cyan]\n"
        "[dim]Tự động giải và nộp bài onluyen.vn với Gemini SDK & Jev Classifier[/dim]",
        border_style="cyan"
    ))

    # Đọc cấu hình từ .env
    env_config = load_env_config()
    saved_u = env_config.get("ONLUYEN_USERNAME", "")
    saved_p = env_config.get("ONLUYEN_PASSWORD", "")
    saved_gemini = env_config.get("GEMINI_API_KEY", "")

    # Đăng nhập
    client = OnluyenClient()
    if not saved_u or not saved_p:
        console.print("[yellow]Chưa tìm thấy tài khoản trong file .env, vui lòng nhập:[/yellow]")
        username = Prompt.ask("[bold green]Tài khoản Onluyen (SĐT/Email)[/bold green]", default=saved_u)
        password = Prompt.ask("[bold green]Mật khẩu Onluyen[/bold green]", default=saved_p, password=True)
        # Lưu vào .env để lần sau không phải nhập lại
        save_env_config({"ONLUYEN_USERNAME": username, "ONLUYEN_PASSWORD": password})
    else:
        username = saved_u
        password = saved_p
        console.print(f"[dim]Đã nạp tài khoản lưu trong .env: {username}[/dim]")

    with console.status("[cyan]Đang đăng nhập Onluyen.vn...[/cyan]"):
        if not client.login(username, password):
            console.print("[bold red]Đăng nhập thất bại! Vui lòng kiểm tra lại tài khoản trong file .env.[/bold red]")
            # Xóa cache nếu tài khoản sai
            username = Prompt.ask("[bold green]Nhập lại tài khoản[/bold green]")
            password = Prompt.ask("[bold green]Nhập lại mật khẩu[/bold green]", password=True)
            if client.login(username, password):
                save_env_config({"ONLUYEN_USERNAME": username, "ONLUYEN_PASSWORD": password})
            else:
                return

    console.print(f"[bold green]✓ Đăng nhập thành công:[/bold green] {client.display_name or client.username} (Lớp ID: {client.school_id})")

    # Khởi tạo Solver
    solver: Optional[BaseSolver] = None
    solver_mode = env_config.get("DEFAULT_SOLVER", "1")

    while True:
        console.print("\n[bold cyan]═══ MENU CHỨC NĂNG ═══[/bold cyan]")
        console.print("  [1] [bold]Bắt đầu giải bài tập tự động[/bold]")
        console.print("  [2] [bold]Xem kết quả các bài đã nộp[/bold]")
        console.print("  [3] [bold]Cấu hình AI Solver (Gemini / Jev Classifier)[/bold]")
        console.print("  [4] [bold]Đổi tài khoản Onluyen[/bold]")
        console.print("  [0] [dim]Thoát chương trình[/dim]")

        choice = Prompt.ask("[bold green]Chọn chức năng[/bold green]", choices=["1", "2", "3", "4", "0"], default="1")

        if choice == "0":
            console.print("[cyan]Tạm biệt![/cyan]")
            break

        elif choice == "3" or (choice == "1" and solver is None):
            console.print("\n[bold yellow]Chọn AI Solver để giải đề:[/bold yellow]")
            console.print("  [1] [bold]Google Gemini API (Official google-genai SDK)[/bold] (Độ chính xác cao)")
            console.print("  [2] [bold]Jev Classifier API (classifier.dev)[/bold] (Miễn phí, zero-shot classifier, không cần API key)")
            s_choice = Prompt.ask("Lựa chọn", choices=["1", "2"], default=solver_mode)

            if s_choice == "1":
                gemini_key = saved_gemini or os.environ.get("GEMINI_API_KEY", "")
                if not gemini_key:
                    gemini_key = Prompt.ask("[bold green]Nhập Gemini API Key (từ Google AI Studio)[/bold green]")
                    if gemini_key:
                        save_env_config({"GEMINI_API_KEY": gemini_key, "DEFAULT_SOLVER": "1"})
                        saved_gemini = gemini_key
                if not gemini_key:
                    console.print("[red]Cần có Gemini API Key để sử dụng chế độ này.[/red]")
                    continue
                solver = GeminiSolver(api_key=gemini_key)
                save_env_config({"DEFAULT_SOLVER": "1"})
                console.print("[green]✓ Đã lưu và cấu hình Gemini Solver (gemini-3.8-flash)[/green]")
            else:
                solver = JevClassifierSolver()
                save_env_config({"DEFAULT_SOLVER": "2"})
                console.print("[green]✓ Đã cấu hình Jev Classifier (classifier.dev)[/green]")

            if choice == "3":
                continue

        if choice == "1":
            if solver is None:
                # Mặc định Jev nếu chưa cấu hình
                solver = JevClassifierSolver()
            do_solve_workflow(client, solver)

        elif choice == "2":
            do_view_results(client)

        elif choice == "4":
            new_u = Prompt.ask("[bold green]Nhập tài khoản mới[/bold green]")
            new_p = Prompt.ask("[bold green]Nhập mật khẩu mới[/bold green]", password=True)
            if client.login(new_u, new_p):
                save_env_config({"ONLUYEN_USERNAME": new_u, "ONLUYEN_PASSWORD": new_p})
                console.print(f"[bold green]✓ Đã đăng nhập và lưu tài khoản mới:[/bold green] {client.display_name}")
            else:
                console.print("[bold red]Đăng nhập thất bại với tài khoản mới![/bold red]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Đã hủy thao tác.[/yellow]")
        sys.exit(0)
