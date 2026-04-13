# -*- coding: utf-8 -*-
import io

import pandas as pd
from fastapi import HTTPException, UploadFile


def is_supported_tabular_file(filename: str | None) -> bool:
    name = (filename or "").lower()
    return name.endswith((".xlsx", ".xls", ".csv"))


async def read_upload_table(file: UploadFile) -> pd.DataFrame:
    if not is_supported_tabular_file(file.filename):
        raise HTTPException(status_code=400, detail="仅支持 Excel (.xlsx, .xls) 或 CSV 文件")

    content = await file.read()
    filename = (file.filename or "").lower()

    try:
        if filename.endswith(".csv"):
            df = None
            last_err = None
            for enc in ("utf-8-sig", "gbk", "utf-8"):
                try:
                    df = pd.read_csv(io.BytesIO(content), encoding=enc)
                    break
                except Exception as exc:
                    last_err = exc
            if df is None:
                raise HTTPException(status_code=400, detail=f"CSV 读取失败，请确认编码为 UTF-8/GBK: {last_err}")
            return df.where(pd.notnull(df), None)

        df = pd.read_excel(io.BytesIO(content))
        return df.where(pd.notnull(df), None)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文件读取失败: {exc}")
