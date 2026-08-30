"""Request and response shapes.

Pydantic validates the shape only. The limits (empty text, text too long, batch
too large, bad beam size) are checked by hand in main.py so they come back as
400/413 with a stable `error` code, instead of pydantic's 422 with its own
envelope.
"""

from pydantic import BaseModel, Field


class TranslateRequest(BaseModel):
    text: str = Field(..., description="Text to translate. May contain several sentences.")
    source_lang: str | None = Field(
        None, description="FLORES-200 or ISO code. Defaults to eng_Latn."
    )
    target_lang: str | None = Field(
        None, description="FLORES-200 or ISO code. Defaults to spa_Latn."
    )
    beam_size: int | None = Field(None, description="Beam width. Defaults to 4.")
    postedit: bool | None = Field(
        None, description="Apply the LATAM Spanish post-edition list. Defaults to on for spa_Latn."
    )


class TranslateResponse(BaseModel):
    text: str
    source_lang: str
    target_lang: str
    sentences: int = Field(..., description="Sentences actually sent to the model.")
    passed_through: int = Field(
        ..., description="Chunks returned untranslated (a URL, an oversized token)."
    )
    beam_size: int
    postedit_applied: bool
    elapsed_ms: int


class BatchTranslateRequest(BaseModel):
    texts: list[str] = Field(..., description="Texts to translate, order preserved.")
    source_lang: str | None = None
    target_lang: str | None = None
    beam_size: int | None = None
    postedit: bool | None = None


class BatchTranslateResponse(BaseModel):
    translations: list[str]
    count: int
    source_lang: str
    target_lang: str
    sentences: int
    passed_through: int
    beam_size: int
    postedit_applied: bool
    elapsed_ms: int


class HealthResponse(BaseModel):
    status: str = Field(..., description="ok | degraded | error")
    detail: str | None = None
    model: dict
    gpu: dict
    languages: dict
    limits: dict
