"""PDF 텍스트 추출 전용 자식 프로세스 함수. 네트워크와 DB에는 접근하지 않는다."""


def extract_pdf_bytes(pdf_bytes, max_pages):
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    try:
        if doc.page_count > max_pages:
            raise ValueError(f'PDF 페이지 수가 제한({max_pages})을 초과했습니다: {doc.page_count}')
        parts = [page.get_text() for page in doc]
    finally:
        doc.close()
    cursor = 0
    offsets = []
    for part in parts:
        cursor += len(part) + 1
        offsets.append(cursor)
    return {'text': '\n'.join(parts), 'page_offsets': offsets, 'pages': len(parts)}


def worker_entry(pdf_bytes, max_pages, result_pipe):
    try:
        result_pipe.send(('ok', extract_pdf_bytes(pdf_bytes, max_pages)))
    except Exception as exc:  # child boundary: serialize only a safe error message
        result_pipe.send(('error', str(exc)))
    finally:
        result_pipe.close()
