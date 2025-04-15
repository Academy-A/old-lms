import logging
import random
from collections.abc import MutableSequence, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

from lms.generals.enums import DistributionErrorMessage
from lms.generals.models.distribution import DistributionParams
from lms.generals.models.reviewer import Reviewer
from lms.utils.distribution.utils import NameGen

log = logging.getLogger(__name__)


class StudentDistributeData(NamedTuple):
    vk_id: int
    first_name: str
    last_name: str
    homework_id: int
    soho_id: int

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class SohoHomework(BaseModel):
    student_homework_id: int
    student_soho_id: int
    sent_to_review_at: datetime
    chat_url: str
    student_vk_id: int | None
    homework_id: int


class StudentHomework(BaseModel):
    student_name: str
    student_vk_id: int
    student_soho_id: int
    submission_url: str
    homework_id: int
    sent_to_review_at: datetime


class ErrorHomework(BaseModel):
    homework: StudentHomework
    error_message: DistributionErrorMessage


class DistributionReviewer(Reviewer):
    recheck: bool = False
    student_homeworks: list[StudentHomework] = Field(default_factory=list)
    actual: int = 0
    percent: float = 0.0

    @property
    def optimal_desired(self) -> int:
        return min(self.desired, self.abs_max)

    @property
    def optimal_max(self) -> int:
        return min(self.max_, self.abs_max)


class Distribution(BaseModel):
    created_at: datetime
    params: DistributionParams
    homeworks: Sequence[SohoHomework]
    reviewers: Sequence[DistributionReviewer]
    filtered_homeworks: MutableSequence[StudentHomework]
    error_homeworks: MutableSequence[ErrorHomework]
    new_folder_id: str | None = None

    @property
    def name_gen(self) -> NameGen:
        return NameGen(
            base_name=self.params.name,
            dt=self.created_at,
            homework_ids=self.params.homework_ids,
        )

    @property
    def sheet_title(self) -> str:
        return self.name_gen.sheet_title

    @property
    def folder_title(self) -> str:
        return self.name_gen.folder_title

    async def distribute(self) -> None:
        random.shuffle(self.filtered_homeworks)
        await self._distribute_minimum()
        await self._distribute_premium()
        await self._distribute_main()
        await self._distribute_rechecks()

    async def _distribute_minimum(self) -> None:
        for r in self.reviewers:
            actual_rev_min = r.min_ - len(r.student_homeworks)
            for _ in range(actual_rev_min):
                if self.filtered_homeworks:
                    r.student_homeworks.append(self.filtered_homeworks.pop())
                else:
                    break
        log.info(
            "Finish min distribution. Left %d homeworks",
            len(self.filtered_homeworks),
        )
        self._log_reviewers("minimum")

    async def _distribute_premium(self) -> None:
        pass

    async def _distribute_main(self) -> None:
        self._calculate_actual()
        self._filled_main()
        self._log_reviewers("main")

    async def _distribute_rechecks(self) -> None:
        pass

    def _calculate_actual(self) -> None:  # noqa: C901
        homeworks_count = len(self.filtered_homeworks)
        total_desired = sum(r.optimal_desired for r in self.reviewers)
        total_max = sum(r.optimal_max for r in self.reviewers)
        if homeworks_count <= total_desired:
            for r in self.reviewers:
                r.percent = r.optimal_desired / total_desired
        else:
            for r in self.reviewers:
                r.percent = r.optimal_max / total_max

        if homeworks_count < total_desired or homeworks_count < total_max:
            actual = 0
            for r in self.reviewers:
                r.actual = int(r.percent * homeworks_count)
                actual += r.actual
            r_ind = 0
            for _ in range(homeworks_count - actual):
                while True:
                    if (
                        self.reviewers[r_ind].actual
                        < self.reviewers[r_ind].optimal_desired
                    ):
                        self.reviewers[r_ind].actual += 1
                        break
                    r_ind += 1
                    r_ind %= len(self.reviewers)
        elif homeworks_count == total_desired:
            for r in self.reviewers:
                r.actual = r.optimal_desired
        else:
            for r in self.reviewers:
                r.actual = r.optimal_max

    def _filled_main(self) -> None:
        total_actual = sum(r.actual for r in self.reviewers)
        hws = self.filtered_homeworks[:total_actual]
        count = 5
        while len(hws) and count > 0:
            k = 0
            while k < len(hws):
                hw = hws.pop()
                for r in self.reviewers:
                    if r.actual > len(r.student_homeworks):
                        r.student_homeworks.append(hw)
                        hw = None  # type: ignore[assignment]
                        break
                if hw is not None:
                    hws.insert(0, hw)
                    k += 1
            count -= 1
        for hw in hws:
            for r in self.reviewers:
                if r.actual < len(r.student_homeworks):
                    r.student_homeworks.append(hw)
        for hw in self.filtered_homeworks[total_actual:]:
            self.error_homeworks.append(
                ErrorHomework(
                    homework=hw,
                    error_message=DistributionErrorMessage.STACK_OVERFLOW,
                )
            )
        self.filtered_homeworks.clear()

    def serialize_for_sheet(self) -> Sequence[Sequence[Any]]:
        data: list[list[str | int]] = [
            [
                self.params.name,
                "",
                f"НАЗВАНИЕ ФАЙЛА: 1234567_Имя_Фамилия_{self.params.name}",
                "",
                "Проверенные в ЧАТ с РЕБЕНКОМ + В ПАПКУ:",
                f"https://drive.google.com/drive/folders/{self.new_folder_id}",
                "",
                "",
                "",
                "",
                "ДЕДЛАЙН В СУББОТУ, 23:59",
            ],
            [],
        ]
        counter = 0
        for r in self.reviewers:
            counter += 1
            column_identify: list[str | int] = [
                r.first_name + " " + r.last_name,
                "Максимум ",
                "Факт:",
                "Всего",
                "VK ID",
            ]
            column_name: list[int | str] = [
                r.id,
                r.optimal_max,
                len(r.student_homeworks),
                f"=COUNTA(OFFSET(A2;4;{3 + (6 * (counter - 1))};200;1))",
                "Имя Фамилия",
            ]
            column_homework_id: list[int | str] = [
                "",
                "",
                "",
                "",
                "Homework ID",
            ]
            column_sent_at: list[int | str] = [
                "",
                "",
                "",
                "",
                "Сдана в",
            ]
            r.student_homeworks.sort(key=lambda hw: hw.sent_to_review_at)
            for hw in r.student_homeworks:
                column_identify.append(str(hw.student_vk_id))
                column_name.append(
                    f'=HYPERLINK("{hw.submission_url}";"{hw.student_name}")'
                )
                column_homework_id.append(hw.homework_id)
                column_sent_at.append(
                    hw.sent_to_review_at.strftime("%d.%m.%Y %H:%M:%S")
                )
            data.extend(
                [
                    column_identify,
                    column_name,
                    column_homework_id,
                    column_sent_at,
                    [],
                    [],
                ]
            )
        error_identify: list[int | str] = ["Домашние работы с ошибками"]
        error_name: list[str | int] = [""]
        error_message: list[str | int] = [""]
        for e_hw in self.error_homeworks:
            error_identify.append(e_hw.homework.student_soho_id)
            error_name.append(
                f'=HYPERLINK("{e_hw.homework.submission_url}";"Ссылка на работу")'
            )
            error_message.append(e_hw.error_message)
        data.extend([[], error_identify, error_name, error_message])
        return data

    def _log_reviewers(self, step: str) -> None:
        log_str = [f"Reviewers on step `{step}`:"]
        for r in self.reviewers:
            log_str.append(f"{r.name}: {len(r.student_homeworks)},")
        log.info(" ".join(log_str))
