# Mantenimiento del repositorio — actualizado 2026-08-17

## Publicación y automatización

- Origen canónico: `https://github.com/LynxPardelle/zoolanding-integrations`.
- Ramas base publicadas: `main`, `test` y `dev`; promoción `dev -> test -> main`.
- CI y Environments usan permisos mínimos y ramas exactas. Roles
  OIDC/CloudFormation y topic de alarmas están copiados a secretos de cada
  Environment, sin claves AWS estáticas; las variables duplicadas fueron
  eliminadas tras verificar correctamente la CI del commit `b7ec35d`. Todos los
  demás valores live del despliegue también se leen exclusivamente desde
  `secrets.*`.
- `.gitleaks.toml` conserva reglas por defecto y exceptúa únicamente dos valores
  sintéticos exactos dentro de un archivo de pruebas; no excluye archivos ni
  reglas completas.
- Auditoría de publicación corregida: el fallback de metadata Stripe exige la
  sesión canónica, los secretos del webhook se leen sólo después del rechazo
  básico, rutas browser tienen límites de coste, SNS usa cifrado administrado y
  las colas de fallo tienen alarmas de edad/profundidad.
- CI cancela ejecuciones obsoletas, tiene timeouts y ejecuta Gitleaks fijado por
  SHA sobre el historial completo.
- Validación local: 312/312 pruebas, compilación, SAM, Actionlint y Gitleaks.

## Despliegue pendiente

**NO-GO para desplegar la aplicación.** Sólo existen las identidades retenidas.
Faltan los parámetros SSM y stacks de servicios, callers SMTP aprobados, hash de
claim compartido y límites Stripe respaldados por evidencia. Esos valores no se
inventaron. El topic de alarmas tiene cero suscriptores confirmados.

También quedan dos controles de publicación: configurar un aprobador
independiente en los GitHub Environments y reemplazar la resolución transitiva
de `pip` por archivos lock con hashes compatibles con Python 3.13/SAM. Las
dependencias directas sí están fijadas; no se generó un lock amplio sin validar
su portabilidad Linux/arm64.

La protección mediante rulesets y la visibilidad pública se configuran sólo
después de verificar esta rama. Use pull requests, CI y pushes normales; nunca
fuerce historia.

No transfiera `.env`, secretos SMTP/Stripe, payloads, URLs alojadas, PII,
`.aws-sam`, bytecode, cachés ni entornos virtuales. Clone el código desde GitHub.
