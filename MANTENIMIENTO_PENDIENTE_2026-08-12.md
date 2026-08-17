# Mantenimiento del repositorio — actualizado 2026-08-17

## Publicación y automatización

- Origen canónico privado: `https://github.com/LynxPardelle/zoolanding-integrations`.
- Ramas base publicadas: `main`, `test` y `dev`; promoción `dev -> test -> main`.
- CI y Environments usan permisos mínimos y ramas exactas. Los roles
  OIDC/CloudFormation y el topic de alarmas ya están configurados sin claves AWS
  estáticas.
- `.gitleaks.toml` conserva reglas por defecto y exceptúa únicamente dos valores
  sintéticos exactos dentro de un archivo de pruebas; no excluye archivos ni
  reglas completas.
- Validación local: 306/306 pruebas, compilación, SAM, Actionlint y Gitleaks.

## Despliegue pendiente

**NO-GO para desplegar la aplicación.** Sólo existen las identidades retenidas.
Faltan los parámetros SSM y stacks de servicios, callers SMTP aprobados, hash de
claim compartido y límites Stripe respaldados por evidencia. Esos valores no se
inventaron. El topic de alarmas tiene cero suscriptores confirmados.

La protección de ramas privadas requiere un plan GitHub superior o hacer el
repositorio público; se preservó la privacidad. Use pull requests, CI y pushes
normales; nunca fuerce historia.

No transfiera `.env`, secretos SMTP/Stripe, payloads, URLs alojadas, PII,
`.aws-sam`, bytecode, cachés ni entornos virtuales. Clone el código desde GitHub.
