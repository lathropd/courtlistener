import datetime
import json
import unittest
from functools import wraps
from typing import Sized, cast

import scorched
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.test.testcases import SerializeMixin
from django.test.utils import override_settings
from django.utils import timezone
from lxml import etree
from requests import Session

from cl.audio.factories import AudioFactory
from cl.audio.models import Audio
from cl.people_db.factories import (
    ABARatingFactory,
    AttorneyFactory,
    AttorneyOrganizationFactory,
    EducationFactory,
    PartyFactory,
    PartyTypeFactory,
    PersonFactory,
    PoliticalAffiliationFactory,
    PositionFactory,
    SchoolFactory,
)
from cl.people_db.models import Person, Race
from cl.search.docket_sources import DocketSources
from cl.search.documents import DocketDocument
from cl.search.factories import (
    CitationWithParentsFactory,
    CourtFactory,
    DocketEntryWithParentsFactory,
    DocketFactory,
    OpinionClusterFactory,
    OpinionFactory,
    OpinionsCitedWithParentsFactory,
    RECAPDocumentFactory,
)
from cl.search.models import (
    Citation,
    Court,
    Docket,
    Opinion,
    OpinionsCitedByRECAPDocument,
    RECAPDocument,
)
from cl.search.tasks import add_items_to_solr
from cl.tests.cases import SimpleTestCase, TestCase
from cl.users.factories import UserProfileWithParentsFactory


def midnight_pt_test(d: datetime.date) -> datetime.datetime:
    """Cast a naive date object to midnight Pacific Time, either PST or PDT,
    according to the date. This method also considers historical timezone
    offsets, similar to how they are handled in DRF.
    """
    time_zone = timezone.get_current_timezone()
    d = datetime.datetime.combine(d, datetime.time())
    return timezone.make_aware(d, time_zone)


opinion_cluster_v3_v4_common_fields = {
    "absolute_url": lambda x: x["result"].cluster.get_absolute_url(),
    "attorney": lambda x: x["result"].cluster.attorneys,
    "caseName": lambda x: x["result"].cluster.case_name,
    "citation": lambda x: [
        str(cite) for cite in x["result"].cluster.citations.all()
    ],
    "citeCount": lambda x: x["result"].cluster.citation_count,
    "court": lambda x: x["result"].cluster.docket.court.full_name,
    "court_citation_string": lambda x: x[
        "result"
    ].cluster.docket.court.citation_string,
    "court_id": lambda x: x["result"].cluster.docket.court_id,
    "cluster_id": lambda x: x["result"].cluster_id,
    "docketNumber": lambda x: x["result"].cluster.docket.docket_number,
    "docket_id": lambda x: x["result"].cluster.docket_id,
    "judge": lambda x: x["result"].cluster.judges,
    "lexisCite": lambda x: (
        str(x["result"].cluster.citations.filter(type=Citation.LEXIS)[0])
        if x["result"].cluster.citations.filter(type=Citation.LEXIS)
        else ""
    ),
    "neutralCite": lambda x: (
        str(x["result"].cluster.citations.filter(type=Citation.NEUTRAL)[0])
        if x["result"].cluster.citations.filter(type=Citation.NEUTRAL)
        else ""
    ),
    "scdb_id": lambda x: x["result"].cluster.scdb_id,
    "sibling_ids": lambda x: list(
        x["result"].cluster.sub_opinions.all().values_list("id", flat=True)
    ),
    "status": lambda x: x["status"],
    "suitNature": lambda x: x["result"].cluster.nature_of_suit,
}

opinion_document_v3_v4_common_fields = {
    "author_id": lambda x: x["result"].author_id,
    "cites": lambda x: (
        list(
            x["result"]
            .cited_opinions.all()
            .values_list("cited_opinion_id", flat=True)
        )
        if x["result"]
        .cited_opinions.all()
        .values_list("cited_opinion_id", flat=True)
        else None
    ),
    "download_url": lambda x: x["result"].download_url,
    "id": lambda x: x["result"].pk,
    "joined_by_ids": lambda x: (
        list(x["result"].joined_by.all().values_list("id", flat=True))
        if x["result"].joined_by.all()
        else None
    ),
    "type": lambda x: x["type"],
    "local_path": lambda x: (
        x["result"].local_path if x["result"].local_path else None
    ),
    "per_curiam": lambda x: x["result"].per_curiam,
    "snippet": lambda x: x["snippet"],
}

opinion_cluster_v3_fields = opinion_cluster_v3_v4_common_fields.copy()
opinion_document_v3_fields = opinion_document_v3_v4_common_fields.copy()

opinion_v3_search_api_keys = {
    "court_exact": lambda x: x["result"].cluster.docket.court_id,
    "panel_ids": lambda x: (
        list(x["result"].cluster.panel.all().values_list("id", flat=True))
        if x["result"].cluster.panel.all()
        else None
    ),
    "dateArgued": lambda x: (
        midnight_pt_test(x["result"].cluster.docket.date_argued).isoformat()
        if x["result"].cluster.docket.date_argued
        else None
    ),
    "dateFiled": lambda x: (
        midnight_pt_test(x["result"].cluster.date_filed).isoformat()
        if x["result"].cluster.date_filed
        else None
    ),
    "dateReargued": lambda x: (
        midnight_pt_test(x["result"].cluster.docket.date_reargued).isoformat()
        if x["result"].cluster.docket.date_reargued
        else None
    ),
    "dateReargumentDenied": lambda x: (
        midnight_pt_test(
            x["result"].cluster.docket.date_reargument_denied
        ).isoformat()
        if x["result"].cluster.docket.date_reargument_denied
        else None
    ),
    "date_created": lambda x: timezone.localtime(
        x["result"].cluster.date_created
    ).isoformat(),
    "timestamp": lambda x: timezone.localtime(
        x["result"].cluster.date_created
    ).isoformat(),
}
opinion_v3_search_api_keys.update(opinion_cluster_v3_fields)
opinion_v3_search_api_keys.update(opinion_document_v3_fields)


opinion_v4_search_api_keys = {
    "non_participating_judge_ids": lambda x: (
        list(
            x["result"]
            .cluster.non_participating_judges.all()
            .values_list("id", flat=True)
        )
    ),
    "source": lambda x: x["result"].cluster.source,
    "caseNameFull": lambda x: x["result"].cluster.case_name_full,
    "panel_names": lambda x: [
        judge.name_full for judge in x["result"].cluster.panel.all()
    ],
    "procedural_history": lambda x: x["result"].cluster.procedural_history,
    "posture": lambda x: x["result"].cluster.posture,
    "syllabus": lambda x: x["result"].cluster.syllabus,
    "opinions": [],  # type: ignore,
    "panel_ids": lambda x: (
        list(x["result"].cluster.panel.all().values_list("id", flat=True))
        if x["result"].cluster.panel.all()
        else []
    ),
    "dateArgued": lambda x: (
        x["result"].cluster.docket.date_argued.isoformat()
        if x["result"].cluster.docket.date_argued
        else None
    ),
    "dateFiled": lambda x: (
        x["result"].cluster.date_filed.isoformat()
        if x["result"].cluster.date_filed
        else None
    ),
    "dateReargued": lambda x: (
        x["result"].cluster.docket.date_reargued.isoformat()
        if x["result"].cluster.docket.date_reargued
        else None
    ),
    "dateReargumentDenied": lambda x: (
        x["result"].cluster.docket.date_reargument_denied.isoformat()
        if x["result"].cluster.docket.date_reargument_denied
        else None
    ),
    "date_created": lambda x: x["result"]
    .cluster.date_created.isoformat()
    .replace("+00:00", "Z"),
    "timestamp": lambda x: x["result"]
    .cluster.date_created.isoformat()
    .replace("+00:00", "Z"),
}

opinion_cluster_v4_common_fields = opinion_cluster_v3_v4_common_fields.copy()
opinion_v4_search_api_keys.update(opinion_cluster_v4_common_fields)

opinion_document_v4_api_keys = {
    "sha1": lambda x: x["result"].sha1,
}
opinion_document_v4_api_keys.update(opinion_document_v3_v4_common_fields)


docket_v4_api_keys_base = {
    "assignedTo": lambda x: (
        x["assignedTo"]
        if x.get("assignedTo")
        else (
            x["result"].docket_entry.docket.assigned_to.name_full
            if x["result"].docket_entry.docket.assigned_to
            else None
        )
    ),
    "assigned_to_id": lambda x: (
        x["result"].docket_entry.docket.assigned_to.pk
        if x["result"].docket_entry.docket.assigned_to
        else None
    ),
    "attorney": lambda x: list(
        DocketDocument().prepare_parties(x["result"].docket_entry.docket)[
            "attorney"
        ]
    ),
    "attorney_id": lambda x: list(
        DocketDocument().prepare_parties(x["result"].docket_entry.docket)[
            "attorney_id"
        ]
    ),
    "caseName": lambda x: (
        x["caseName"]
        if x.get("caseName")
        else x["result"].docket_entry.docket.case_name
    ),
    "case_name_full": lambda x: x["result"].docket_entry.docket.case_name_full,
    "cause": lambda x: (
        x["cause"] if x.get("cause") else x["result"].docket_entry.docket.cause
    ),
    "chapter": lambda x: (
        x["result"].docket_entry.docket.bankruptcy_information.chapter
        if hasattr(x["result"].docket_entry.docket, "bankruptcy_information")
        else None
    ),
    "court": lambda x: x["result"].docket_entry.docket.court.full_name,
    "court_citation_string": lambda x: (
        x["court_citation_string"]
        if x.get("court_citation_string")
        else x["result"].docket_entry.docket.court.citation_string
    ),
    "court_exact": lambda x: x["result"].docket_entry.docket.court.pk,
    "court_id": lambda x: x["result"].docket_entry.docket.court.pk,
    "dateArgued": lambda x: (
        x["result"].docket_entry.docket.date_argued.isoformat()
        if x["result"].docket_entry.docket.date_argued
        else None
    ),
    "dateFiled": lambda x: (
        x["result"].docket_entry.docket.date_filed.isoformat()
        if x["result"].docket_entry.docket.date_filed
        else None
    ),
    "dateTerminated": lambda x: (
        x["result"].docket_entry.docket.date_terminated.isoformat()
        if x["result"].docket_entry.docket.date_terminated
        else None
    ),
    "date_created": lambda x: x["result"]
    .docket_entry.docket.date_created.isoformat()
    .replace("+00:00", "Z"),
    "docketNumber": lambda x: (
        x["docketNumber"]
        if x.get("docketNumber")
        else x["result"].docket_entry.docket.docket_number
    ),
    "docket_absolute_url": lambda x: x[
        "result"
    ].docket_entry.docket.get_absolute_url(),
    "docket_id": lambda x: x["result"].docket_entry.docket_id,
    "firm": lambda x: list(
        DocketDocument().prepare_parties(x["result"].docket_entry.docket)[
            "firm"
        ]
    ),
    "firm_id": lambda x: list(
        DocketDocument().prepare_parties(x["result"].docket_entry.docket)[
            "firm_id"
        ]
    ),
    "jurisdictionType": lambda x: x[
        "result"
    ].docket_entry.docket.jurisdiction_type,
    "juryDemand": lambda x: (
        x["juryDemand"]
        if x.get("juryDemand")
        else x["result"].docket_entry.docket.jury_demand
    ),
    "pacer_case_id": lambda x: x["result"].docket_entry.docket.pacer_case_id,
    "party": lambda x: list(
        DocketDocument().prepare_parties(x["result"].docket_entry.docket)[
            "party"
        ]
    ),
    "party_id": lambda x: list(
        DocketDocument().prepare_parties(x["result"].docket_entry.docket)[
            "party_id"
        ]
    ),
    "referredTo": lambda x: (
        x["referredTo"]
        if x.get("referredTo")
        else (
            x["result"].docket_entry.docket.referred_to.name_full
            if x["result"].docket_entry.docket.referred_to
            else None
        )
    ),
    "referred_to_id": lambda x: (
        x["result"].docket_entry.docket.referred_to.pk
        if x["result"].docket_entry.docket.referred_to
        else None
    ),
    "suitNature": lambda x: (
        x["suitNature"]
        if x.get("suitNature")
        else x["result"].docket_entry.docket.nature_of_suit
    ),
    "timestamp": lambda x: x["result"]
    .docket_entry.docket.date_created.isoformat()
    .replace("+00:00", "Z"),
    "trustee_str": lambda x: (
        x["result"].docket_entry.docket.bankruptcy_information.trustee_str
        if hasattr(x["result"].docket_entry.docket, "bankruptcy_information")
        else None
    ),
}

docket_v4_api_keys = docket_v4_api_keys_base.copy()
docket_v4_api_keys.update(
    {
        "more_docs": lambda x: False,
        "recap_documents": [],  # type: ignore
    }
)

recap_document_v4_api_keys = {
    "id": lambda x: x["result"].pk,
    "docket_entry_id": lambda x: x["result"].docket_entry.pk,
    "description": lambda x: (
        x["description"]
        if x.get("description")
        else x["result"].docket_entry.description
    ),
    "entry_number": lambda x: x["result"].docket_entry.entry_number,
    "entry_date_filed": lambda x: (
        x["result"].docket_entry.date_filed.isoformat()
        if x["result"].docket_entry.date_filed
        else None
    ),
    "short_description": lambda x: (
        x["short_description"]
        if x.get("short_description")
        else x["result"].description
    ),
    "document_type": lambda x: x["result"].get_document_type_display(),
    "document_number": lambda x: (
        int(x["result"].document_number)
        if x["result"].document_number
        else None
    ),
    "pacer_doc_id": lambda x: x["result"].pacer_doc_id or "",
    "snippet": lambda x: (
        x["snippet"] if x.get("snippet") else x["result"].plain_text or ""
    ),
    "attachment_number": lambda x: x["result"].attachment_number or None,
    "is_available": lambda x: x["result"].is_available,
    "page_count": lambda x: x["result"].page_count or None,
    "filepath_local": lambda x: x["result"].filepath_local.name or None,
    "absolute_url": lambda x: x["result"].get_absolute_url(),
    "cites": lambda x: list(
        x["result"]
        .cited_opinions.all()
        .values_list("cited_opinion_id", flat=True)
    ),
    "timestamp": lambda x: x["result"]
    .date_created.isoformat()
    .replace("+00:00", "Z"),
}

rd_type_v4_api_keys = recap_document_v4_api_keys.copy()
rd_type_v4_api_keys.update(
    {
        "docket_id": lambda x: x["result"].docket_entry.docket_id,
    }
)


class CourtTestCase(SimpleTestCase):
    """Court test case factories"""

    @classmethod
    def setUpTestData(cls):
        cls.court_1 = CourtFactory(
            id="ca1",
            full_name="First Circuit",
            jurisdiction="F",
            citation_string="1st Cir.",
            url="http://www.ca1.uscourts.gov/",
        )
        cls.court_2 = CourtFactory(
            id="test",
            full_name="Testing Supreme Court",
            jurisdiction="F",
            citation_string="Test",
            url="https://www.courtlistener.com/",
        )
        super().setUpTestData()


class PeopleTestCase(SimpleTestCase):
    """People test case factories"""

    @classmethod
    def setUpTestData(cls):
        cls.w_race, _ = Race.objects.get_or_create(race="w")
        cls.b_race, _ = Race.objects.get_or_create(race="b")
        cls.person_1 = PersonFactory.create(
            gender="m",
            name_first="Bill",
            name_last="Clinton",
        )
        cls.person_1.race.add(cls.w_race)

        cls.person_2 = PersonFactory.create(
            gender="f",
            name_first="Judith",
            name_last="Sheindlin",
            date_dob=datetime.date(1942, 10, 21),
            date_dod=datetime.date(2020, 11, 25),
            date_granularity_dob="%Y-%m-%d",
            date_granularity_dod="%Y-%m-%d",
            name_middle="Susan",
            dob_city="Brookyln",
            dob_state="NY",
        )
        cls.person_2.race.add(cls.w_race)
        cls.person_2.race.add(cls.b_race)

        cls.person_3 = PersonFactory.create(
            gender="f",
            name_first="Sheindlin",
            name_last="Judith",
            date_dob=datetime.date(1945, 11, 20),
            date_granularity_dob="%Y-%m-%d",
            name_middle="Olivia",
            dob_city="Queens",
            dob_state="NY",
        )
        cls.person_3.race.add(cls.w_race)

        cls.position_1 = PositionFactory.create(
            date_granularity_start="%Y-%m-%d",
            date_start=datetime.date(1993, 1, 20),
            date_retirement=datetime.date(2001, 1, 20),
            termination_reason="retire_mand",
            position_type="pres",
            person=cls.person_1,
            how_selected="e_part",
        )
        cls.position_2 = PositionFactory.create(
            date_granularity_start="%Y-%m-%d",
            court=cls.court_1,
            date_start=datetime.date(2015, 12, 14),
            predecessor=cls.person_2,
            appointer=cls.position_1,
            judicial_committee_action="no_rep",
            termination_reason="retire_mand",
            position_type="c-jud",
            person=cls.person_2,
            how_selected="e_part",
            nomination_process="fed_senate",
            date_elected=datetime.date(2015, 11, 12),
        )
        cls.position_3 = PositionFactory.create(
            date_granularity_start="%Y-%m-%d",
            date_start=datetime.date(2015, 12, 14),
            organization_name="Pants, Inc.",
            job_title="Corporate Lawyer",
            position_type=None,
            person=cls.person_2,
        )
        cls.position_4 = PositionFactory.create(
            date_granularity_start="%Y-%m-%d",
            court=cls.court_2,
            date_start=datetime.date(2020, 12, 14),
            predecessor=cls.person_3,
            appointer=cls.position_1,
            judicial_committee_action="no_rep",
            termination_reason="retire_mand",
            position_type="c-jud",
            person=cls.person_3,
            how_selected="a_legis",
            nomination_process="fed_senate",
        )

        cls.school_1 = SchoolFactory(name="New York Law School")
        cls.school_2 = SchoolFactory(name="American University")

        cls.education_1 = EducationFactory(
            degree_level="jd",
            person=cls.person_2,
            degree_year=1965,
            school=cls.school_1,
        )
        cls.education_2 = EducationFactory(
            degree_level="ba",
            person=cls.person_2,
            school=cls.school_2,
        )
        cls.education_3 = EducationFactory(
            degree_level="ba",
            person=cls.person_3,
            school=cls.school_1,
        )

        cls.political_affiliation_1 = PoliticalAffiliationFactory.create(
            political_party="d",
            source="b",
            date_start=datetime.date(1993, 1, 1),
            person=cls.person_1,
            date_granularity_start="%Y",
        )
        cls.political_affiliation_2 = PoliticalAffiliationFactory.create(
            political_party="d",
            source="b",
            date_start=datetime.date(2015, 12, 14),
            person=cls.person_2,
            date_granularity_start="%Y-%m-%d",
        )
        cls.political_affiliation_3 = PoliticalAffiliationFactory.create(
            political_party="i",
            source="b",
            date_start=datetime.date(2015, 12, 14),
            person=cls.person_3,
            date_granularity_start="%Y-%m-%d",
        )

        cls.aba_rating_1 = ABARatingFactory(
            rating="nq",
            person=cls.person_2,
            year_rated="2015",
        )
        super().setUpTestData()


class SearchTestCase(SimpleTestCase):
    """Search test case factories"""

    @classmethod
    def setUpTestData(cls):
        cls.docket_1 = DocketFactory.create(
            date_reargument_denied=datetime.date(2015, 8, 15),
            court_id=cls.court_2.pk,
            case_name_full="case name full docket 1",
            date_argued=datetime.date(2015, 8, 16),
            case_name="case name docket 1",
            case_name_short="case name short docket 1",
            docket_number="docket number 1 005",
            slug="case-name",
            pacer_case_id="666666",
            blocked=False,
            source=0,
        )
        cls.docket_2 = DocketFactory.create(
            date_reargument_denied=datetime.date(2015, 8, 15),
            court_id=cls.court_2.pk,
            case_name_full="case name full docket 2",
            date_argued=datetime.date(2015, 8, 15),
            case_name="case name docket 2",
            case_name_short="case name short docket 2",
            docket_number="docket number 2",
            slug="case-name",
            blocked=False,
            source=0,
        )
        cls.docket_3 = DocketFactory.create(
            date_reargument_denied=datetime.date(2015, 8, 15),
            court_id=cls.court_1.pk,
            case_name_full="case name full docket 3",
            date_argued=datetime.date(2015, 8, 14),
            case_name="case name docket 3",
            case_name_short="case name short docket 3",
            docket_number="docket number 3",
            slug="case-name",
            blocked=False,
            source=0,
        )

        cls.opinion_cluster_1 = OpinionClusterFactory.create(
            case_name_full="Paul Debbas v. Franklin",
            case_name_short="Debbas",
            syllabus="some rando syllabus",
            date_filed=datetime.date(2015, 8, 14),
            procedural_history="some rando history",
            source="C",
            case_name="Debbas v. Franklin",
            attorneys="a bunch of crooks!",
            slug="case-name-cluster",
            precedential_status="Errata",
            citation_count=4,
            docket=cls.docket_1,
        )
        cls.opinion_cluster_1.panel.add(cls.person_2)

        cls.opinion_cluster_2 = OpinionClusterFactory.create(
            case_name_full="Harvey Howard v. Antonin Honda",
            case_name_short="Howard",
            syllabus="some rando syllabus",
            date_filed=datetime.date(1895, 6, 9),
            procedural_history="some rando history",
            source="C",
            judges="David",
            case_name="Howard v. Honda",
            attorneys="a bunch of crooks!",
            slug="case-name-cluster",
            precedential_status="Published",
            citation_count=6,
            scdb_votes_minority=3,
            scdb_votes_majority=6,
            nature_of_suit="copyright",
            docket=cls.docket_2,
        )

        cls.opinion_cluster_3 = OpinionClusterFactory.create(
            case_name_full="Reference to Lissner v. Saad",
            case_name_short="case name short cluster 3",
            syllabus="some rando syllabus",
            date_filed=datetime.date(2015, 8, 15),
            procedural_history="some rando history",
            source="C",
            case_name="case name cluster 3",
            attorneys="a bunch of crooks!",
            slug="case-name-cluster",
            precedential_status="Published",
            citation_count=8,
            docket=cls.docket_3,
        )

        cls.citation_1 = CitationWithParentsFactory.create(
            volume=33,
            reporter="state",
            page="1",
            type=1,
            cluster=cls.opinion_cluster_1,
        )
        cls.citation_2 = CitationWithParentsFactory.create(
            volume=22,
            reporter="AL",
            page="339",
            type=8,
            cluster=cls.opinion_cluster_2,
        )
        cls.citation_3 = CitationWithParentsFactory.create(
            volume=33,
            reporter="state",
            page="1",
            type=1,
            cluster=cls.opinion_cluster_2,
        )
        cls.citation_4 = CitationWithParentsFactory.create(
            volume=1,
            reporter="Yeates",
            page="1",
            type=5,
            cluster=cls.opinion_cluster_2,
        )
        cls.citation_5 = CitationWithParentsFactory.create(
            volume=56,
            reporter="F.2d",
            page="9",
            type=1,
            cluster=cls.opinion_cluster_2,
        )
        cls.citation_5 = CitationWithParentsFactory.create(
            volume=56,
            reporter="F.2d",
            page="11",
            type=1,
            cluster=cls.opinion_cluster_3,
        )
        cls.opinion_1 = OpinionFactory.create(
            extracted_by_ocr=False,
            author=cls.person_2,
            plain_text="my plain text secret word for queries",
            cluster=cls.opinion_cluster_1,
            local_path="test/search/opinion_doc.doc",
            per_curiam=False,
            type="020lead",
        )
        cls.opinion_2 = OpinionFactory.create(
            extracted_by_ocr=False,
            author=cls.person_2,
            plain_text="my plain text secret word for queries",
            cluster=cls.opinion_cluster_2,
            html_with_citations='yadda yadda <span class="star-pagination">*9</span> this is page 9 <span class="star-pagination">*10</span> this is content on page 10 can we link to it...',
            local_path="test/search/opinion_pdf_image_based.pdf",
            per_curiam=False,
            type="010combined",
        )
        cls.opinion_2.joined_by.add(cls.person_2)

        cls.opinion_3 = OpinionFactory.create(
            extracted_by_ocr=False,
            author=cls.person_2,
            plain_text="my plain text secret word for queries 1 Yeates 1",
            cluster=cls.opinion_cluster_3,
            local_path="test/search/opinion_pdf_text_based.pdf",
            per_curiam=False,
            type="010combined",
        )
        cls.opinion_4 = OpinionFactory.create(
            extracted_by_ocr=False,
            author=cls.person_2,
            plain_text="my plain text secret word for queries",
            cluster=cls.opinion_cluster_1,
            local_path="test/search/opinion_html.html",
            per_curiam=False,
            type="010combined",
        )
        cls.opinion_5 = OpinionFactory.create(
            extracted_by_ocr=False,
            author=cls.person_2,
            plain_text="my plain text secret word for queries",
            cluster=cls.opinion_cluster_1,
            local_path="test/search/opinion_wpd.wpd",
            per_curiam=False,
            type="010combined",
        )
        cls.opinion_6 = OpinionFactory.create(
            extracted_by_ocr=False,
            author=cls.person_2,
            plain_text="my plain text secret word for queries",
            cluster=cls.opinion_cluster_1,
            local_path="test/search/opinion_text.txt",
            per_curiam=False,
            type="010combined",
        )
        cls.opinion_cited_1 = OpinionsCitedWithParentsFactory.create(
            cited_opinion=cls.opinion_2,
            citing_opinion=cls.opinion_1,
        )
        cls.opinion_cited_2 = OpinionsCitedWithParentsFactory.create(
            cited_opinion=cls.opinion_3,
            citing_opinion=cls.opinion_1,
        )
        cls.opinion_cited_3 = OpinionsCitedWithParentsFactory.create(
            cited_opinion=cls.opinion_3,
            citing_opinion=cls.opinion_2,
        )
        cls.opinion_cited_4 = OpinionsCitedWithParentsFactory.create(
            cited_opinion=cls.opinion_1,
            citing_opinion=cls.opinion_3,
        )
        super().setUpTestData()


class RECAPSearchTestCase(SimpleTestCase):
    """RECAP Search test case factories"""

    @classmethod
    def setUpTestData(cls):
        cls.court = CourtFactory(id="canb", jurisdiction="FB")
        cls.court_2 = CourtFactory(id="ca1", jurisdiction="F")
        cls.judge = PersonFactory.create(
            name_first="Thalassa", name_last="Miller"
        )
        cls.judge_2 = PersonFactory.create(
            name_first="Persephone", name_last="Sinclair"
        )
        cls.de = DocketEntryWithParentsFactory(
            docket=DocketFactory(
                court=cls.court,
                case_name="SUBPOENAS SERVED ON",
                case_name_full="Jackson & Sons Holdings vs. Bank",
                date_filed=datetime.date(2015, 8, 16),
                date_argued=datetime.date(2013, 5, 20),
                docket_number="1:21-bk-1234",
                assigned_to=cls.judge,
                referred_to=cls.judge_2,
                nature_of_suit="440",
                source=Docket.RECAP,
                cause="401 Civil",
                jurisdiction_type="'U.S. Government Defendant",
                jury_demand="1,000,000",
            ),
            entry_number=1,
            date_filed=datetime.date(2015, 8, 19),
            description="MOTION for Leave to File Amicus Curiae Lorem",
        )
        cls.firm = AttorneyOrganizationFactory(name="Associates LLP")
        cls.attorney = AttorneyFactory(
            name="Debbie Russell",
            organizations=[cls.firm],
            docket=cls.de.docket,
        )
        cls.party_type = PartyTypeFactory.create(
            party=PartyFactory(
                name="Defendant Jane Roe",
                docket=cls.de.docket,
                attorneys=[cls.attorney],
            ),
            docket=cls.de.docket,
        )

        cls.rd = RECAPDocumentFactory(
            docket_entry=cls.de,
            description="Leave to File",
            document_number="1",
            is_available=True,
            page_count=5,
            pacer_doc_id="018036652435",
        )

        cls.opinion = OpinionFactory(
            cluster=OpinionClusterFactory(docket=cls.de.docket)
        )
        OpinionsCitedByRECAPDocument.objects.bulk_create(
            [
                OpinionsCitedByRECAPDocument(
                    citing_document=cls.rd,
                    cited_opinion=cls.opinion,
                    depth=1,
                )
            ]
        )
        cls.rd_att = RECAPDocumentFactory(
            docket_entry=cls.de,
            description="Document attachment",
            document_type=RECAPDocument.ATTACHMENT,
            document_number="1",
            attachment_number=2,
            is_available=False,
            page_count=7,
            pacer_doc_id="018036652436",
        )

        cls.judge_3 = PersonFactory.create(
            name_first="Seraphina", name_last="Hawthorne"
        )
        cls.judge_4 = PersonFactory.create(
            name_first="Leopold", name_last="Featherstone"
        )
        cls.de_1 = DocketEntryWithParentsFactory(
            docket=DocketFactory(
                docket_number="12-1235",
                court=cls.court_2,
                case_name="SUBPOENAS SERVED OFF",
                case_name_full="The State of Franklin v. Solutions LLC",
                date_filed=datetime.date(2016, 8, 16),
                date_argued=datetime.date(2012, 6, 23),
                assigned_to=cls.judge_3,
                referred_to=cls.judge_4,
                source=Docket.COLUMBIA_AND_RECAP,
            ),
            entry_number=3,
            date_filed=datetime.date(2014, 7, 19),
            description="MOTION for Leave to File Amicus Discharging Debtor",
        )
        cls.rd_2 = RECAPDocumentFactory(
            docket_entry=cls.de_1,
            description="Leave to File",
            document_number="3",
            page_count=10,
            plain_text="Mauris iaculis, leo sit amet hendrerit vehicula, Maecenas nunc justo. Integer varius sapien arcu, quis laoreet lacus consequat vel.",
            pacer_doc_id="016156723121",
        )
        super().setUpTestData()


class SerializeLockFileTestMixin(SerializeMixin):
    lockfile = __file__


class SimpleUserDataMixin:
    @classmethod
    def setUpTestData(cls) -> None:
        UserProfileWithParentsFactory.create(
            user__username="pandora",
            user__password=make_password("password"),
        )
        super().setUpTestData()  # type: ignore


@override_settings(
    SOLR_OPINION_URL=settings.SOLR_OPINION_TEST_URL,
    SOLR_AUDIO_URL=settings.SOLR_AUDIO_TEST_URL,
    SOLR_PEOPLE_URL=settings.SOLR_PEOPLE_TEST_URL,
    SOLR_RECAP_URL=settings.SOLR_RECAP_TEST_URL,
    SOLR_URLS=settings.SOLR_TEST_URLS,
    ELASTICSEARCH_DISABLED=True,
)
class EmptySolrTestCase(SerializeLockFileTestMixin, TestCase):
    """Sets up an empty Solr index for tests that need to set up data manually.

    Other Solr test classes subclass this one, adding additional content or
    features.
    """

    def setUp(self) -> None:
        # Set up testing cores in Solr and swap them in
        self.core_name_opinion = settings.SOLR_OPINION_TEST_CORE_NAME
        self.core_name_audio = settings.SOLR_AUDIO_TEST_CORE_NAME
        self.core_name_people = settings.SOLR_PEOPLE_TEST_CORE_NAME
        self.core_name_recap = settings.SOLR_RECAP_TEST_CORE_NAME

        self.session = Session()

        self.si_opinion = scorched.SolrInterface(
            settings.SOLR_OPINION_URL, http_connection=self.session, mode="rw"
        )
        self.si_audio = scorched.SolrInterface(
            settings.SOLR_AUDIO_URL, http_connection=self.session, mode="rw"
        )
        self.si_people = scorched.SolrInterface(
            settings.SOLR_PEOPLE_URL, http_connection=self.session, mode="rw"
        )
        self.si_recap = scorched.SolrInterface(
            settings.SOLR_RECAP_URL, http_connection=self.session, mode="rw"
        )
        self.all_sis = [
            self.si_opinion,
            self.si_audio,
            self.si_people,
            self.si_recap,
        ]

    def tearDown(self) -> None:
        try:
            for si in self.all_sis:
                si.delete_all()
                si.commit()
        finally:
            self.session.close()


class SolrTestCase(
    CourtTestCase,
    PeopleTestCase,
    SearchTestCase,
    SimpleUserDataMixin,
    EmptySolrTestCase,
):
    """A standard Solr test case with content included in the database,  but not
    yet indexed into the database.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

    def setUp(self) -> None:
        # Set up some handy variables
        super().setUp()

        self.court = Court.objects.get(pk="test")
        self.expected_num_results_opinion = 6
        self.expected_num_results_audio = 2


class IndexedSolrTestCase(SolrTestCase):
    """Similar to the SolrTestCase, but the data is indexed in Solr"""

    def setUp(self) -> None:
        super().setUp()
        obj_types = {
            "audio.Audio": Audio,
            "search.Opinion": Opinion,
            "people_db.Person": Person,
        }
        for obj_name, obj_type in obj_types.items():
            if obj_name == "people_db.Person":
                items = obj_type.objects.filter(is_alias_of=None)
                ids = [item.pk for item in items if item.is_judge]
            else:
                ids = obj_type.objects.all().values_list("pk", flat=True)
            add_items_to_solr(ids, obj_name, force_commit=True)


class SitemapTest(TestCase):
    sitemap_url: str
    expected_item_count: int

    def assert_sitemap_has_content(self) -> None:
        """Does content get into the sitemap?"""
        response = self.client.get(self.sitemap_url)
        self.assertEqual(
            200, response.status_code, msg="Did not get a 200 OK status code."
        )
        xml_tree = etree.fromstring(response.content)
        node_count = len(
            cast(
                Sized,
                xml_tree.xpath(
                    "//s:url",
                    namespaces={
                        "s": "http://www.sitemaps.org/schemas/sitemap/0.9"
                    },
                ),
            )
        )
        self.assertGreater(
            self.expected_item_count,
            0,
            msg="Didn't get any content in test case.",
        )
        self.assertEqual(
            node_count,
            self.expected_item_count,
            msg="Did not get the right number of items in the sitemap.\n"
            f"\tCounted:\t{node_count}\n"
            f"\tExpected:\t{self.expected_item_count}",
        )


class AudioTestCase(SimpleTestCase):
    """Audio test case factories"""

    @classmethod
    def setUpTestData(cls):
        cls.audio_1 = AudioFactory.create(
            docket_id=1,
            duration=420,
            judges="",
            local_path_original_file="test/audio/ander_v._leo.mp3",
            local_path_mp3="test/audio/2.mp3",
            sha1="de8cff186eb263dc06bdc5340860eb6809f898d3",
            source="C",
            blocked=False,
        )
        cls.audio_2 = AudioFactory.create(
            docket_id=2,
            duration=837,
            judges="",
            local_path_original_file="mp3/2014/06/09/ander_v._leo.mp3",
            local_path_mp3="test/audio/2.mp3",
            sha1="daadaf6cc018114259f7eba27c4c2e6bba9bd0d7",
            source="C",
        )
        cls.audio_3 = AudioFactory.create(
            docket_id=3,
            duration=653,
            judges="",
            local_path_original_file="mp3/2015/07/08/hong_liu_yang_v._loretta_e._lynch.mp3",
            local_path_mp3="test/audio/2.mp3",
            sha1="f540838e606f15585e713812c67537affc0df944",
            source="CR",
        )

    @classmethod
    def tearDownClass(cls):
        Audio.objects.all().delete()
        super().tearDownClass()


class AudioESTestCase(SimpleTestCase):
    """Audio test case factories for ES"""

    fixtures = [
        "test_court.json",
        "judge_judy.json",
        "test_objects_search.json",
    ]

    @classmethod
    def setUpTestData(cls):
        cls.court_1 = CourtFactory(
            id="cabc",
            full_name="Testing Supreme Court",
            jurisdiction="FB",
            citation_string="Bankr. C.D. Cal.",
        )
        cls.court_2 = CourtFactory(
            id="nyed",
            full_name="Court of Appeals for the First Circuit",
            jurisdiction="FB",
            citation_string="Bankr. C.D. Cal.",
        )
        cls.docket_1 = DocketFactory.create(
            docket_number="1:21-bk-1234",
            court_id=cls.court_1.pk,
            date_argued=datetime.date(2015, 8, 16),
        )
        cls.docket_2 = DocketFactory.create(
            docket_number="19-5734",
            court_id=cls.court_1.pk,
            date_argued=datetime.date(2015, 8, 15),
        )
        cls.docket_3 = DocketFactory.create(
            docket_number="ASBCA No. 59126",
            court_id=cls.court_2.pk,
            date_argued=datetime.date(2015, 8, 14),
        )
        cls.docket_4 = DocketFactory.create(
            docket_number="1:21-cv-1234-ABC",
            court_id=cls.court_1.pk,
            date_argued=datetime.date(2013, 8, 14),
        )
        transcript_response = {
            "response": {
                "results": [
                    {
                        "alternatives": [
                            {
                                "transcript": "This is the best transcript.",
                                "confidence": 0.85,
                            },
                            {
                                "transcript": "Another possible transcript.",
                                "confidence": 0.75,
                            },
                        ]
                    },
                ]
            }
        }
        json_transcript = json.dumps(transcript_response)
        cls.audio_1 = AudioFactory.create(
            case_name="SEC v. Frank J. Information, WikiLeaks",
            docket_id=cls.docket_1.pk,
            duration=420,
            judges="Mary Deposit Learning rd Administrative procedures act",
            local_path_original_file="test/audio/ander_v._leo.mp3",
            local_path_mp3="test/audio/2.mp3",
            source="C",
            blocked=False,
            sha1="a49ada009774496ac01fb49818837e2296705c97",
            stt_status=Audio.STT_COMPLETE,
            stt_google_response=json_transcript,
        )
        cls.audio_2 = AudioFactory.create(
            case_name="Jose A. Dominguez v. Loretta E. Lynch",
            docket_id=cls.docket_2.pk,
            duration=837,
            judges="Wallace and Friedland Learn of rd",
            local_path_original_file="mp3/2014/06/09/ander_v._leo.mp3",
            local_path_mp3="test/audio/2.mp3",
            source="C",
            sha1="a49ada009774496ac01fb49818837e2296705c92",
        )
        cls.audio_3 = AudioFactory.create(
            case_name="Hong Liu Yang v. Lynch-Loretta E.",
            docket_id=cls.docket_3.pk,
            duration=653,
            judges="Joseph Information Deposition H Administrative magazine",
            local_path_original_file="mp3/2015/07/08/hong_liu_yang_v._loretta_e._lynch.mp3",
            local_path_mp3="test/audio/2.mp3",
            source="CR",
            sha1="a49ada009774496ac01fb49818837e2296705c93",
        )
        cls.author = PersonFactory.create()
        cls.audio_4 = AudioFactory.create(
            case_name="Hong Liu Lorem v. Lynch-Loretta E.",
            docket_id=cls.docket_3.pk,
            duration=653,
            judges="John Smith ptsd mag",
            sha1="a49ada009774496ac01fb49818837e2296705c94",
        )
        cls.audio_4.panel.add(cls.author)
        cls.audio_5 = AudioFactory.create(
            case_name="Freedom of Inform Wikileaks",
            docket_id=cls.docket_4.pk,
            duration=400,
            judges="Wallace to Friedland ⚖️ Deposit xx-xxxx apa magistrate",
            sha1="a49ada009774496ac01fb49818837e2296705c95",
        )


def skip_if_common_tests_skipped(method):
    """Decorator to skip common tests based on the skip_common_tests attribute."""

    @wraps(method)
    async def wrapper_func(self, *args, **kwargs):
        if getattr(self, "skip_common_tests", False):
            raise unittest.SkipTest("Skip common tests within the class.")
        return await method(self, *args, **kwargs)

    return wrapper_func


def generate_docket_target_sources(
    initial_sources: list[int], incoming_source: int
) -> dict[str, str]:
    """Generates a mapping for testing of docket target sources based on
    initial sources and an incoming source.

    :param initial_sources: A list of integers representing the initial source
     values.
    :param incoming_source: An integer representing the incoming source value
    to be added to each of the initial sources.
    :return: A dict mapping from initial source names to target source names,
    based on the sum of the initial source value and the incoming source value.
    e.g: {"RECAP": "COLUMBIA_AND_RECAP"}
    """
    inverse_sources = {
        value: key
        for key, value in DocketSources.__dict__.items()
        if not key.startswith("__") and isinstance(value, int)
    }

    target_sources = {}
    for source in initial_sources:
        target_source = source + incoming_source
        if incoming_source == Docket.RECAP and source == Docket.DEFAULT:
            # Exception for  DEFAULT + RECAP source assignation.
            target_sources[inverse_sources[source]] = "RECAP_AND_SCRAPER"
        else:
            target_sources[inverse_sources[source]] = inverse_sources[
                target_source
            ]

    return target_sources
