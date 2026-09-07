"""OIDC session auth and the four roles (12 §6, 13 §2b).

Two rules here carry real weight, and both are enforced as dependencies rather than
checked ad hoc in handlers:

* **Sign-off and contract confirmation require Data Owner, not Engineer.** If the
  monitoring team can sign off on its own alerts, the board gets rubber-stamped green —
  the adoption failure named as risk C1.
* **Every mutating action records the acting user.** `grain_confirmed_by`, `signed_by`,
  `answered_by` and `submitted_by` are people, resolved from the session, never a
  service account.

There are no local accounts. `DPHM_DEV_AUTH=1` grants an admin dev session so the app is
runnable before an OIDC client exists (Q21); it refuses to activate when
`DPHM_ENV=production`, because a dev bypass that survives to production is how
attribution silently becomes fiction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class Role(IntEnum):
    """Ordered, so a dependency can express "at least this role" (12 §6)."""

    VIEWER = 10
    ENGINEER = 20
    DATA_OWNER = 30
    ADMIN = 40

    @classmethod
    def parse(cls, value: str) -> Role:
        try:
            return cls[value.strip().upper().replace("-", "_")]
        except KeyError as exc:
            raise ValueError(
                f"unknown role {value!r}; expected one of {', '.join(r.name.lower() for r in cls)}"
            ) from exc


@dataclass(frozen=True)
class Principal:
    """The signed-in human. Never a service account for a mutating action."""

    subject: str
    email: str
    role: Role
    groups: tuple[str, ...] = ()

    @property
    def is_dev_session(self) -> bool:
        return self.subject.startswith("dev:")

    def can(self, required: Role) -> bool:
        return self.role >= required


# Group -> role mapping, from the OIDC provider's claims (Q21).
# `DPHM_ROLE_MAP="grp-data-owners:data_owner,grp-platform:admin"`
def _group_role_map() -> dict[str, Role]:
    raw = os.environ.get("DPHM_ROLE_MAP", "")
    out: dict[str, Role] = {}
    for entry in raw.split(","):
        if ":" not in entry:
            continue
        group, role = entry.split(":", 1)
        if group.strip():
            out[group.strip()] = Role.parse(role)
    return out


def role_for_groups(groups: tuple[str, ...]) -> Role:
    """Highest role any of the user's groups grants. Defaults to Viewer.

    Defaulting to Viewer rather than Engineer is deliberate: read access is harmless,
    and an unmapped group should not silently acquire the ability to trigger spend.
    """
    mapping = _group_role_map()
    granted = [mapping[g] for g in groups if g in mapping]
    return max(granted) if granted else Role.VIEWER


def _dev_principal() -> Principal | None:
    if os.environ.get("DPHM_DEV_AUTH") != "1":
        return None
    if os.environ.get("DPHM_ENV", "").lower() in {"production", "prod"}:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "DPHM_DEV_AUTH is set in a production environment. Refusing to serve: a "
                "dev auth bypass would make every sign-off unattributable (13 §2b)."
            ),
        )
    return Principal(
        subject="dev:local",
        email=os.environ.get("DPHM_DEV_EMAIL", "dev@localhost"),
        role=Role.ADMIN,
        groups=("dev",),
    )


async def current_principal(request: Request) -> Principal:
    """Resolve the session. 401 when there is none."""
    dev = _dev_principal()
    if dev is not None:
        return dev

    session = getattr(request, "session", None) or {}
    subject = session.get("sub")
    email = session.get("email")
    if not subject or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not signed in — SSO via the company OIDC provider; there are no local accounts",
        )
    groups = tuple(session.get("groups") or ())
    return Principal(
        subject=str(subject), email=str(email), role=role_for_groups(groups), groups=groups
    )


def requires(role: Role) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: `Depends(requires(Role.DATA_OWNER))`.

    The refusal names the role needed, so the UI can disable the control *before* the
    click rather than explaining the failure after it (10, 12 §2.6).
    """

    async def _dep(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not principal.can(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"this action requires the {role.name.lower()} role; "
                    f"you have {principal.role.name.lower()}"
                ),
            )
        return principal

    return _dep


CurrentUser = Annotated[Principal, Depends(current_principal)]
Viewer = Annotated[Principal, Depends(requires(Role.VIEWER))]
Engineer = Annotated[Principal, Depends(requires(Role.ENGINEER))]
DataOwner = Annotated[Principal, Depends(requires(Role.DATA_OWNER))]
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
