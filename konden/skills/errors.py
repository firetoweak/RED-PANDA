class SkillInputError(ValueError):
    """Skill 控制面能够确定的外部输入或持久状态错误。"""


class SkillNotFoundError(SkillInputError):
    pass


class SkillAlreadyInstalledError(SkillInputError):
    pass


class SkillPreconditionError(SkillInputError):
    pass


class SkillInstalledPackageError(SkillInputError, RuntimeError):
    pass


class SkillCandidateNotFoundError(SkillInputError):
    pass
