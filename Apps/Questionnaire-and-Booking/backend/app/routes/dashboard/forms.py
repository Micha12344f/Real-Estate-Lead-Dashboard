"""Staff edits and read-only applicant form configuration, both behind proxy auth."""
from fastapi import APIRouter, HTTPException, Response
from ...services.questionnaire import forms

router = APIRouter()


@router.get('/dashboard/forms')
@router.get('/webhook/thames-valley-estates/forms-config')
async def get_forms(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await forms.read()
    except Exception as error:
        raise HTTPException(503, 'Form settings are unavailable. Please retry.') from error


@router.put('/dashboard/forms')
async def put_forms(body: dict, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await forms.save(body)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except Exception as error:
        raise HTTPException(503, 'Form settings could not be saved. Check the settings store and retry.') from error
