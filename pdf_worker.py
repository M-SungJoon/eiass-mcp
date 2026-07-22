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


def extract_pdf_path(pdf_path, max_pages):
    """디스크에 스트리밍된 PDF를 열어 대형 파일의 프로세스 간 bytes 복사를 피한다."""
    import fitz

    doc = fitz.open(pdf_path)
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


def extract_pdf_path_to_file(pdf_path, max_pages, text_path):
    """페이지 텍스트를 즉시 파일로 써 자식 프로세스의 list+join 대형 복사를 없앤다."""
    import os
    import fitz

    part_path = text_path + '.part'
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count > max_pages:
            raise ValueError(f'PDF 페이지 수가 제한({max_pages})을 초과했습니다: {doc.page_count}')
        cursor = 0
        offsets = []
        with open(part_path, 'w', encoding='utf-8', newline='\n') as stream:
            for index, page in enumerate(doc):
                text = page.get_text()
                if index:
                    stream.write('\n')
                stream.write(text)
                cursor += len(text) + 1
                offsets.append(cursor)
        os.replace(part_path, text_path)
        return {'page_offsets': offsets, 'pages': doc.page_count, 'text_chars': max(0, cursor - 1)}
    finally:
        doc.close()
        try:
            os.remove(part_path)
        except OSError:
            pass


def worker_entry(pdf_bytes, max_pages, result_pipe):
    try:
        result_pipe.send(('ok', extract_pdf_bytes(pdf_bytes, max_pages)))
    except Exception as exc:  # child boundary: serialize only a safe error message
        result_pipe.send(('error', str(exc)))
    finally:
        result_pipe.close()


def worker_entry_path(pdf_path, max_pages, result_pipe, text_path=None):
    try:
        payload = (extract_pdf_path_to_file(pdf_path, max_pages, text_path)
                   if text_path else extract_pdf_path(pdf_path, max_pages))
        result_pipe.send(('ok', payload))
    except Exception as exc:
        result_pipe.send(('error', str(exc)))
    finally:
        result_pipe.close()
