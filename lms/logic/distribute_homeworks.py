from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from aiomisc import threaded
from google_api_service_helper import GoogleDrive, GoogleSheets

from lms.adapters.db.uow import UnitOfWork
from lms.adapters.soho.soho import Soho
from lms.generals.models.distribution import DistributionParams
from lms.generals.models.subject import Subject
from lms.utils.distribution.models import (
    Distribution,
    DistributionReviewer,
    ErrorHomework,
    SohoHomework,
    StudentDistributeData,
    StudentHomework,
)

SHEET_INDEX: Final[int] = 4
SHEET_MAJOR_DIMENSION: Final[str] = "COLUMNS"


@dataclass(frozen=True, slots=True)
class Distributor:
    uow: UnitOfWork
    google_sheets: GoogleSheets
    google_drive: GoogleDrive
    soho: Soho

    async def make_distribution(
        self,
        params: DistributionParams,
        created_at: datetime,
    ) -> None:
        homeworks = await self._get_soho_homeworks(params.homework_ids)
        subject = await self._get_subject(params.product_ids[0])
        reviewers = await self._get_reviewers(subject_id=subject.id)
        student_map = await self._get_student_data_map(homeworks=homeworks)
        filtered_homeworks, error_homeworks = _filter_homeworks(
            homeworks=homeworks,
            student_map=student_map,
        )
        distribution = Distribution(
            created_at=created_at,
            params=params,
            homeworks=homeworks,
            filtered_homeworks=filtered_homeworks,
            error_homeworks=error_homeworks,
            reviewers=reviewers,
        )
        await distribution.distribute()
        new_folder_id = await self._create_folder_for_homeworks(
            parent_folder_id=subject.check_drive_foler_id,
            distribution=distribution,
        )
        distribution.new_folder_id = new_folder_id
        await self._write_data_in_new_sheet(
            spreadsheet_id=subject.check_spreadsheet_id,
            distribution=distribution,
        )
        await self._save_distribution(
            subject_id=subject.id,
            distribution=distribution,
        )
        await self._add_folder_to_notification(
            subject_id=subject.id,
            folder_id=new_folder_id,
        )
        await self.uow.commit()

    async def _get_soho_homeworks(
        self,
        homework_ids: Sequence[int],
    ) -> Sequence[SohoHomework]:
        homeworks: list[SohoHomework] = []
        for homework_id in homework_ids:
            response = await self.soho.homeworks(homework_id)
            for homework in response.homeworks:
                homeworks.append(
                    SohoHomework(**homework.model_dump(), homework_id=homework_id)
                )
        return homeworks

    async def _get_subject(self, product_id: int) -> Subject:
        product = await self.uow.product.read_by_id(product_id=product_id)
        return await self.uow.subject.read_by_id(product.subject_id)

    async def _get_reviewers(self, subject_id: int) -> Sequence[DistributionReviewer]:
        reviewers = await self.uow.reviewer.get_list_by_subject_id(subject_id)
        return [DistributionReviewer(**r.model_dump()) for r in reviewers]

    async def _get_student_data_map(
        self,
        homeworks: Sequence[SohoHomework],
    ) -> Mapping[int, StudentDistributeData]:
        clients = await self.soho.fetch_all_clients()
        student_data_map = {}
        for homework in homeworks:
            for client in clients:
                if client.id == homework.student_soho_id:
                    student_data_map[client.id] = StudentDistributeData(
                        vk_id=homework.student_vk_id or 0,
                        first_name=client.first_name,
                        last_name=client.last_name,
                        homework_id=homework.homework_id,
                        soho_id=client.id,
                    )
                    break
        return student_data_map

    async def _add_folder_to_notification(
        self, subject_id: int, folder_id: str
    ) -> None:
        subject = await self.uow.subject.read_by_id(subject_id=subject_id)
        subject.check_regular_nofitication_folder_ids
        if len(subject.check_regular_nofitication_folder_ids) >= 20:
            del subject.properties.check_regular_notification_folder_ids[0]
        subject.properties.check_regular_notification_folder_ids.append(folder_id)
        subj_dict = subject.model_dump()
        subj_dict["properties"] = subject.properties.model_dump(mode="json")
        await self.uow.subject.update(
            id_=subject.id,
            **subj_dict,
        )

    async def _save_distribution(
        self,
        subject_id: int,
        distribution: Distribution,
    ) -> None:
        data = distribution.model_dump()
        await self.uow.distribution.create(
            subject_id=subject_id,
            data=data,
        )

    @threaded
    def _create_folder_for_homeworks(
        self,
        distribution: Distribution,
        parent_folder_id: str,
    ) -> str:
        new_folder = self.google_drive.make_new_folder(
            new_folder_title=distribution.folder_title,
            parent_folder_id=parent_folder_id,
        )
        if new_folder is None:
            raise ValueError("Folder not created")
        self.google_drive.set_permissions_for_anyone(folder_id=new_folder.id)
        return new_folder.id

    @threaded
    def _write_data_in_new_sheet(
        self,
        distribution: Distribution,
        spreadsheet_id: str,
    ) -> None:
        self.google_sheets.create_sheet(
            spreadsheet_id=spreadsheet_id,
            title=distribution.sheet_title,
            index=SHEET_INDEX,
        )
        self.google_sheets.set_data(
            spreadsheet_id=spreadsheet_id,
            range_sheet=distribution.sheet_title,
            values=distribution.serialize_for_sheet(),
            major_dimension=SHEET_MAJOR_DIMENSION,
        )


def _filter_homeworks(
    homeworks: Sequence[SohoHomework],
    student_map: Mapping[int, StudentDistributeData],
) -> tuple[Sequence[StudentHomework], Sequence[ErrorHomework]]:
    pre_filtered_homeworks: list[StudentHomework] = list()
    error_homeworks: list[ErrorHomework] = list()
    for hw in homeworks:
        sh = StudentHomework(
            student_name=student_map[hw.student_soho_id].name,
            student_vk_id=student_map[hw.student_soho_id].vk_id,
            student_soho_id=hw.student_soho_id,
            submission_url=hw.chat_url,
            homework_id=hw.homework_id,
            sent_to_review_at=hw.sent_to_review_at,
        )
        pre_filtered_homeworks.append(sh)
    return pre_filtered_homeworks, error_homeworks
