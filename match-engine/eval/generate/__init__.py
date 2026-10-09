"""Synthetic candidate profiles, postings and résumé renderers (EVAL.md section 2).

A profile is the ground truth; `render` writes it as a document in one of 8 templates x 6 formats;
`expected_resume` is what a perfect parser recovers from it. Heavy libraries (reportlab, python-docx,
PyMuPDF, Pillow, numpy) are imported lazily, inside the functions that need them.
"""
