# tool-onluyen

Chương trình Terminal UI (TUI) tự động tải đề, giải bài tập và nộp bài trên **onluyen.vn**.

## Tính năng

- **Giao diện dòng lệnh trực quan (Rich TUI)**: Quản lý danh sách bài tập trường học, hiển thị tiến độ giải từng câu (progress bar) và bộ đếm ngược thời gian nộp bài.
- **Hỗ trợ 2 AI Solver**:
  1. **Google Gemini API**: Giải chính xác cao với SDK chính thức `google-genai` (mặc định model `gemini-3.8-flash` kiến trúc Argon 4, hỗ trợ fallback sang `gemini-2.5-flash` / `gemini-2.0-flash`).
  2. **Jev Classifier (`classifier.dev`)**: Zero-shot classifier miễn phí qua `https://classifier.dev/<labels>/<text>`, không cần API key.
- **Tự động lưu cấu hình**: Nhập tài khoản Onluyen hoặc Gemini API key một lần, tool tự động lưu vào file `.env` để tái sử dụng.
- **Hỗ trợ đầy đủ dạng câu hỏi**:
  - Trắc nghiệm 4 đáp án (A, B, C, D).
  - Trắc nghiệm Đúng / Sai.
  - Điền từ / số trả lời ngắn.
- **Tùy chọn nộp bài linh hoạt**:
  - Nộp ngay (tự đợi đủ 3 phút theo quy định hệ thống).
  - Hẹn giờ làm bài (ví dụ đặt 10 phút, 15 phút để thời gian làm bài tự nhiên).
  - Chỉ lưu bài lên server (tự nộp thủ công sau).

## Cài đặt

Yêu cầu Python 3.9+.

```bash
git clone https://github.com/Linhbhi/tool-onluyen.git
cd tool-onluyen
pip install -r requirements.txt
```

## Cấu hình

Tạo file `.env` từ file mẫu:

```bash
cp .env.example .env
```

Điền thông tin vào file `.env`:

```env
ONLUYEN_USERNAME=0987654321
ONLUYEN_PASSWORD=matkhau
GEMINI_API_KEY=AIzaSy...
```

*(Nếu không điền trong `.env`, chương trình sẽ hỏi trực tiếp trên màn hình terminal khi chạy và tự động lưu lại).*

## Sử dụng

Chạy chương trình:

```bash
python3 tool_onluyen.py
```

## License

MIT
