# Orden Digital — Contexto del Proyecto

## 🔧 El Servicio

| Campo | Detalle |
|---|---|
| **Nombre** | Orden Digital |
| **Propuesta de valor** | Automatización de clasificación, renombrado y organización de documentos legales mediante IA |
| **Qué hace técnicamente** | Lee PDFs con Gemini API, los clasifica según los requerimientos del cliente, y los renombra y mueve a carpetas organizadas |

---

## 👥 Mercado Objetivo

- **Segmentos apuntados:** Despachos jurídicos, despachos contables, notarías
- **Cliente prioritario (MVP):** Despachos jurídicos

---

## 💰 Modelo de Negocio y Precios

| Plan | Precio |
|---|---|
| Paquete básico | $800 MXN |
| Paquete estándar | $2,000 MXN |
| Suscripción mensual | $500 MXN |

> ⚠️ **Pendiente:** Definir si se adopta modelo de costo por MB o se mantienen paquetes cerrados.

---

## 🔐 Legal y Confidencialidad

| Elemento | Estado |
|---|---|
| NDA / Acuerdo de Confidencialidad | ⚠️ Pendiente de redacción |
| Figura legal del operador | ⚠️ Pendiente — definir si opera bajo nombre personal o persona moral |

---

## 🛠️ Stack Técnico

### Flujo de trabajo completo

```
Cliente → comparte archivos vía Google Drive
       → descarga local en PC del operador
       → ejecución del script
       → archivos organizados comprimidos en ZIP
       → reenvío al cliente vía Google Drive
```

| Componente | Detalle |
|---|---|
| **API de IA** | Gemini 2.5 Flash Lite (modalidad batch de pago) |
| **Capa de procesamiento** | Local — computadora personal del operador |
| **Transferencia de archivos** | Google Drive (entrada y salida) |
| **Formato de entrega** | ZIP con estructura de carpetas organizadas |

---

## 📣 Marketing y Canal de Ventas

| Canal | Detalle |
|---|---|
| **Contacto principal** | WhatsApp (enlace definitivo ya integrado en el HTML) |
| **Sitio web** | GitHub Pages (solución actual para el MVP) |

> ⚠️ **Pendiente:** Migración a dominio propio en etapa post-MVP.

---
