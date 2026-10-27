#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tool-onluyen: Terminal UI tự động giải và nộp bài tập onluyen.vn
Hỗ trợ solver qua:
  1. Google Gemini API (Gemini 2.5 Flash / 2.0 Flash)
  2. Jev Zero-shot Classifier (classifier.dev)
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
# ONLUYEN API CLIENT
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

    def get_assignments(self, filter_status: str = "todo") -> List[Dict[str, Any]]:
        """Lấy danh sách bài tập được giao."""
        url = f"{self.BASE_API}/assignment/list-assignment-student-by-type"
        params = {
            "pageIndex": 1,
            "pageSize": 50,
            "schoolYear": self.school_year,
            "typeApp": self.code_app
        }
        res = self.session.get(url, params=params, timeout=15)
        if res.status_code != 200:
            return []

        items = res.json().get("data", [])
        result = []
        for item in items:
            st = item.get("status", 0)
            if filter_status == "todo" and st not in (0, 1):
                continue
            if filter_status == "done" and st != 2:
                continue

            result.append({
                "assignId": item.get("assignId"),
                "name": item.get("name"),
                "status": st,
                "statusText": "Chưa làm" if st == 0 else "Đang làm" if st == 1 else "Đã nộp",
                "totalScore": item.get("totalScore"),
                "score": item.get("score"),
                "totalQuestion": item.get("totalQuestion", 0)
            })
        return result

    def get_test_data(self, assign_id: str) -> Optional[Dict[str, Any]]:
        """Lấy dữ liệu đề bài thi."""
        # 1. Kích hoạt start nếu bài mới
        url_start = f"{self.BASE_API}/assignment/student/start"
        self.session.post(url_start, json={"assignId": assign_id}, timeout=15)

        # 2. Tải doing data
        url_doing = f"{self.BASE_API}/assignment/student/get-doing"
        res = self.session.get(url_doing, params={"assignId": assign_id}, timeout=20)
        if res.status_code != 200:
            return None

        return res.json().get("data")

    def upload_to_s3(self, put_url: str, s3_answers: List[Dict[str, Any]]) -> bool:
        """Gửi JSON đáp án trực tiếp lên AWS S3."""
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://app.onluyen.vn",
            "Referer": "https://app.onluyen.vn/"
        }
        res = requests.put(put_url, data=json.dumps(s3_answers, ensure_ascii=False), headers=headers, timeout=20)
        return res.status_code == 200

    def submit(self, assign_id: str, force: bool = False) -> Dict[str, Any]:
        """Gửi lệnh nộp bài."""
        url = f"{self.BASE_API}/assignment/student/submit"
        res = self.session.post(url, json={"assignId": assign_id}, timeout=20)
        if res.status_code == 200:
            return res.json()
        return {"success": False, "message": f"HTTP {res.status_code}: {res.text}"}


# ==============================================================================
# AI SOLVERS: GEMINI & JEV CLASSIFIER
# ==============================================================================
class BaseSolver:
    def solve_choice(self, question: str, options: List[str]) -> str:
        """Trả về chữ cái 'A', 'B', 'C', 'D'."""
        raise NotImplementedError

    def solve_true_false(self, question: str, statements: List[str]) -> List[str]:
        """Trả về mảng ['Đ', 'S', ...] tương ứng với statements."""
        raise NotImplementedError

    def solve_text(self, question: str) -> str:
        """Trả về chuỗi kết quả ngắn."""
        raise NotImplementedError


class JevClassifierSolver(BaseSolver):
    """
    Sử dụng API zero-shot classifier: https://classifier.dev/
    Docs & curl: curl https://classifier.dev/<labels>/<text>
    """
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
    Sử dụng Google Gemini REST API v1beta (hỗ trợ gemini-2.5-flash / gemini-2.0-flash / gemini-1.5-flash).
    Không cần cài đặt thư viện nặng, chạy trơn tru với API key từ Google AI Studio.
    """
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

    def _call(self, prompt: str) -> str:
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 256
            }
        }
        try:
            res = requests.post(self.endpoint, headers=headers, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
            else:
                # Fallback model nếu model 2.5 chưa có sẵn trên tier
                if "gemini-2.5" in self.model:
                    self.model = "gemini-2.0-flash"
                    self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
                    return self._call(prompt)
        except Exception as e:
            console.print(f"[yellow]Gemini API error: {e}[/yellow]")
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
# TUI WORKFLOW
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


def main():
    console.print(Panel.fit(
        "[bold cyan]TOOL ONLUYEN - TUI AUTO SOLVER[/bold cyan]\n"
        "[dim]Tự động giải và nộp bài onluyen.vn với Gemini API & Jev Classifier[/dim]",
        border_style="cyan"
    ))

    # 1. Credentials
    client = OnluyenClient()
    saved_u = os.environ.get("ONLUYEN_USERNAME", "")
    saved_p = os.environ.get("ONLUYEN_PASSWORD", "")

    # Đọc .env nếu có
    if os.path.exists(".env"):
        try:
            with open(".env") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        if k == "ONLUYEN_USERNAME" and not saved_u:
                            saved_u = v
                        elif k == "ONLUYEN_PASSWORD" and not saved_p:
                            saved_p = v
                        elif k == "GEMINI_API_KEY" and "GEMINI_API_KEY" not in os.environ:
                            os.environ["GEMINI_API_KEY"] = v
        except Exception:
            pass

    username = Prompt.ask("[bold green]Tài khoản Onluyen (SĐT/Email)[/bold green]", default=saved_u)
    password = Prompt.ask("[bold green]Mật khẩu Onluyen[/bold green]", default=saved_p, password=True)

    with console.status("[cyan]Đang đăng nhập Onluyen.vn...[/cyan]"):
        if not client.login(username, password):
            console.print("[bold red]Đăng nhập thất bại! Vui lòng kiểm tra lại tài khoản.[/bold red]")
            return

    console.print(f"[bold green]✓ Đăng nhập thành công:[/bold green] {client.display_name or client.username} (ID: {client.user_id})")

    # 2. Chọn Solver
    console.print("\n[bold yellow]Chọn AI Solver để giải đề:[/bold yellow]")
    console.print("  [1] [bold]Google Gemini API[/bold] (Độ chính xác cao, giải toán/lý/hóa/văn cực chuẩn)")
    console.print("  [2] [bold]Jev Classifier API (classifier.dev)[/bold] (Miễn phí, zero-shot classifier, không cần API key)")
    solver_choice = Prompt.ask("Lựa chọn", choices=["1", "2"], default="1")

    solver: BaseSolver
    if solver_choice == "1":
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        key = Prompt.ask("[bold green]Nhập Gemini API Key[/bold green]", default=gemini_key)
        if not key:
            console.print("[red]Cần có Gemini API Key để sử dụng chế độ này.[/red]")
            return
        solver = GeminiSolver(api_key=key)
        console.print("[green]✓ Đã cấu hình Gemini Solver (gemini-2.5-flash)[/green]")
    else:
        solver = JevClassifierSolver()
        console.print("[green]✓ Đã cấu hình Jev Classifier (classifier.dev)[/green]")

    # 3. Lấy danh sách bài tập
    with console.status("[cyan]Đang tải danh sách bài tập chưa làm...[/cyan]"):
        assignments = client.get_assignments(filter_status="todo")

    if not assignments:
        console.print("[yellow]Không tìm thấy bài tập nào chưa làm trong tài khoản![/yellow]")
        return

    table = Table(title="Danh sách bài tập cần làm", border_style="blue")
    table.add_column("STT", style="cyan", width=5)
    table.add_column("Tên bài tập", style="white")
    table.add_column("Số câu", style="magenta", width=8)
    table.add_column("Trạng thái", style="green", width=12)

    for i, a in enumerate(assignments, 1):
        table.add_row(str(i), a["name"], str(a["totalQuestion"]), a["statusText"])

    console.print(table)
    idx_choice = Prompt.ask(f"[bold cyan]Chọn số thứ tự bài muốn làm (1 - {len(assignments)})[/bold cyan]", default="1")
    try:
        chosen_assignment = assignments[int(idx_choice) - 1]
    except Exception:
        console.print("[red]Lựa chọn không hợp lệ.[/red]")
        return

    assign_id = chosen_assignment["assignId"]
    console.print(f"\n[bold]Bắt đầu làm bài:[/bold] [yellow]{chosen_assignment['name']}[/yellow] (ID: {assign_id})")

    # 4. Tải đề
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

    # 5. Giải bài với AI
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
                # Map 'Đ' -> 'true', 'S' -> 'false'
                opt_texts = ["true" if v == "Đ" else "false" for v in tf_list]
                s3_answers.append({
                    "stepId": step_id,
                    "optionId": [],
                    "optionText": opt_texts
                })

            else: # text
                text_ans = solver.solve_text(q["content"])
                s3_answers.append({
                    "stepId": step_id,
                    "optionId": [],
                    "optionText": [text_ans]
                })

            progress.advance(task)

    console.print(f"\n[bold green]✓ Đã hoàn thành giải toàn bộ {len(s3_answers)} câu hỏi![/bold green]")

    # 6. Lưu đáp án lên S3
    with console.status("[cyan]Đang lưu đáp án lên server Onluyen (AWS S3)...[/cyan]"):
        saved = client.upload_to_s3(put_url, s3_answers)

    if saved:
        console.print("[bold green]✓ Đã lưu đáp án thành công lên hệ thống![/bold green]")
    else:
        console.print("[bold red]Không thể lưu đáp án lên S3. Vui lòng kiểm tra lại mạng.[/bold red]")
        return

    # 7. Nộp bài hoặc hẹn giờ
    console.print("\n[bold yellow]Tùy chọn nộp bài:[/bold yellow]")
    console.print("  [1] Nộp ngay (tự động chờ đủ 3 phút tối thiểu theo quy định Onluyen)")
    console.print("  [2] Hẹn giờ làm bài (ví dụ 10 phút, 15 phút rồi mới nộp)")
    console.print("  [3] Chỉ lưu đáp án (tự nộp thủ công sau)")
    submit_mode = Prompt.ask("Lựa chọn", choices=["1", "2", "3"], default="1")

    if submit_mode == "3":
        console.print("[bold green]Đã lưu bài thành công. Bạn có thể mở web để nộp sau.[/bold green]")
        return

    target_wait_seconds = 180  # Mặc định 3 phút
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

    # Gửi lệnh submit
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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Đã hủy thao tác.[/yellow]")
        sys.exit(0)
