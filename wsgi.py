"""Ponto de entrada WSGI para Gunicorn (produção no Render).

Uso: gunicorn wsgi:app
"""
from app import create_app

app = create_app()
