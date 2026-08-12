# Mantenimiento pendiente — 2026-08-12

## Publicación bloqueada de forma segura

Este repositorio no tiene remoto configurado. Se preservaron la historia y las
ramas locales sin crear un repositorio GitHub ni adivinar su visibilidad.

- Destino candidato, sujeto a aprobación: `LynxPardelle/zoolanding-integrations`.
- Visibilidad recomendada hasta la revisión de secretos/proveedores: **privada**.
- Rama actual: `codex/phase8-infrastructure-readiness`.
- Validación local: 306/306 pruebas, compilación Python y
  `sam validate --lint` correctos.
- Despliegue/proveedores: **NO-GO**; no se llamó AWS, Stripe, SMTP ni otro
  proveedor y no se leyó ningún secreto.

Tras aprobar propietario y visibilidad, agregue el `origin` exacto y publique
esta rama con un push normal. Mantenga la promoción `dev -> test -> main` y no
fuerce historia. Las ramas locales de fases anteriores deben conservarse hasta
comprobar su alcance desde el remoto creado.

Para trasladar el repositorio, excluya `.env`, credenciales, secretos SMTP o de
Stripe, payloads de proveedor, URLs alojadas, PII, artefactos SAM y entornos
virtuales. Los valores operativos pertenecen al gestor de secretos aprobado.
